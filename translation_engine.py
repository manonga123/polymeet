"""
translation_engine.py  –  Module F-05/F-06 : Traduction Multilingue Offline
─────────────────────────────────────────────────────────────────────────────
Fonctionnalités :
  • Traduction 100% offline via argos-translate
  • 9 langues : FR, EN, AR, ES, DE, ZH, PT, IT, MG
  • Traduction TOUS AZIMUTS : chaque langue → toutes les autres (72 paires)
  • Détection automatique de la langue source (via Whisper ou langdetect)
  • Fallback via pivot anglais si paire directe absente (ex: mg ↔ ar)
  • Chargement des packs en arrière-plan (non-bloquant)
  • Cache des traductions récentes (évite de re-traduire la même phrase)
  • Callback vers l'UI dès qu'une traduction est prête
  • Glossaire personnalisable (termes métier non traduits)

Installation :
  pip install argos-translate
  python install_languages.py   ← installe les 72 paires
"""

import threading
import queue
import time
import os
import re
import json
from functools import lru_cache

# ── Import argos-translate ───────────────────────────────────────────────────
try:
    import argostranslate.package
    import argostranslate.translate
    ARGOS_AVAILABLE = True
except ImportError:
    ARGOS_AVAILABLE = False
    print("[Translation] ⚠  argos-translate non installé")
    print("               → pip install argos-translate")

# ─── Langues supportées (toutes les 9) ──────────────────────────────────────
# (code_iso, label_affichage, flag)
SUPPORTED_LANGUAGES = [
    ("fr", "Français",  "🇫🇷"),
    ("en", "English",   "🇬🇧"),
    ("mg", "Malagasy",  "🇲🇬"),
    ("ar", "Arabe",     "🇸🇦"),
    ("es", "Espagnol",  "🇪🇸"),
    ("de", "Allemand",  "🇩🇪"),
    ("zh", "Chinois",   "🇨🇳"),
    ("pt", "Portugais", "🇵🇹"),
    ("it", "Italien",   "🇮🇹"),
]

# Tous les codes langues
ALL_LANG_CODES = [code for code, _, _ in SUPPORTED_LANGUAGES]

# Langues avec packs argostranslate disponibles dans l'index officiel
# (Malagasy peut être absent → fallback pivot anglais automatique)
ARGOS_SUPPORTED = {"fr", "en", "ar", "es", "de", "zh", "pt", "it", "mg"}

# Noms lisibles pour les logs
LANG_NAMES = {
    "fr": "Français",
    "en": "Anglais",
    "ar": "Arabe",
    "es": "Espagnol",
    "de": "Allemand",
    "zh": "Chinois",
    "pt": "Portugais",
    "it": "Italien",
    "mg": "Malagasy",
}

# Glossaire par défaut (termes qui ne doivent pas être traduits)
DEFAULT_GLOSSARY = {
    "PolyMeet", "Whisper", "API", "CPU", "GPU", "WiFi",
    "DroidCam", "WebSocket", "Python",
}

# Cache des paires déjà vérifiées comme installées : {(src, tgt): bool}
_installed_pairs: dict = {}

# Fichier glossaire personnalisé
GLOSSARY_FILE = "glossary.json"


class TranslationEngine:
    """
    Moteur de traduction offline argos-translate.
    Supporte la traduction dans TOUTES les directions entre 9 langues.

    Usage :
        engine = TranslationEngine(on_translated=my_callback)
        engine.set_source_language("fr")   # langue détectée par Whisper
        engine.set_target_language("en")   # langue choisie par l'utilisateur
        engine.start()
        engine.translate("Bonjour tout le monde", source_lang="fr")
        engine.stop()

    Callback :
        on_translated(original, translated, source_lang, target_lang)
    """

    def __init__(self, on_translated=None, on_pack_ready=None):
        """
        on_translated(original, translated, source_lang, target_lang)
        on_pack_ready(lang_code)  : appelé quand un pack est prêt
        """
        self.on_translated  = on_translated
        self.on_pack_ready  = on_pack_ready

        self._source_lang   = "auto"     # langue source (auto = détection Whisper)
        self._target_lang   = "fr"       # langue cible par défaut
        self._running       = False
        self._ready         = False
        self._queue         = queue.Queue(maxsize=20)
        self._worker_thread = None
        self._cache         = {}         # {(text, src, tgt): translated}
        self._glossary      = set(DEFAULT_GLOSSARY)
        self._lock          = threading.Lock()

        self._load_glossary()

    # ────────────────────────────────────────────────────────────────────────
    # Démarrage / Arrêt
    # ────────────────────────────────────────────────────────────────────────

    def start(self, source_lang: str = "auto", target_lang: str = "fr"):
        """
        Démarre le moteur de traduction.
        Vérifie/installe les packs nécessaires en arrière-plan.
        """
        if not ARGOS_AVAILABLE:
            print("[Translation] ❌ argos-translate manquant — pip install argos-translate")
            return False

        self._source_lang = source_lang
        self._target_lang = target_lang
        self._running     = True

        # Thread worker de traduction
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="TranslationWorker")
        self._worker_thread.start()

        # Vérifier/installer les packs en arrière-plan
        threading.Thread(
            target=self._ensure_pair_ready,
            args=(source_lang, target_lang),
            daemon=True,
            name="PackInstaller"
        ).start()

        src_name = LANG_NAMES.get(source_lang, source_lang)
        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        print(f"[Translation] ▶ Démarré : {src_name} → {tgt_name}")
        return True

    def stop(self):
        self._running = False
        self._ready   = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
        print("[Translation] Arrêté.")

    # ────────────────────────────────────────────────────────────────────────
    # Gestion des langues source et cible
    # ────────────────────────────────────────────────────────────────────────

    def set_source_language(self, lang_code: str):
        """
        Définit la langue source (habituellement détectée par Whisper).
        Passer "auto" pour laisser argos détecter.
        """
        if lang_code == self._source_lang:
            return
        old = self._source_lang
        self._source_lang = lang_code
        print(f"[Translation] Langue source : {LANG_NAMES.get(old, old)} → {LANG_NAMES.get(lang_code, lang_code)}")

        # Re-vérifier les packs pour la nouvelle paire
        if self._target_lang and lang_code != "auto":
            self._ready = False
            threading.Thread(
                target=self._ensure_pair_ready,
                args=(lang_code, self._target_lang),
                daemon=True
            ).start()

    def set_target_language(self, lang_code: str):
        """
        Change la langue cible à la volée.
        Re-vérifie/installe les packs si nécessaire.
        """
        if lang_code == self._target_lang:
            return
        old = self._target_lang
        self._target_lang = lang_code
        self._ready       = False
        print(f"[Translation] Langue cible : {LANG_NAMES.get(old, old)} → {LANG_NAMES.get(lang_code, lang_code)}")

        threading.Thread(
            target=self._ensure_pair_ready,
            args=(self._source_lang, lang_code),
            daemon=True
        ).start()

    def get_available_targets(self, source_lang: str) -> list:
        """
        Retourne la liste des langues cibles disponibles pour une source donnée.
        Exclut la langue source elle-même.
        """
        return [
            (code, name, flag)
            for code, name, flag in SUPPORTED_LANGUAGES
            if code != source_lang
        ]

    # ────────────────────────────────────────────────────────────────────────
    # Traduction publique
    # ────────────────────────────────────────────────────────────────────────

    def translate(self, text: str, source_lang: str = "auto"):
        """
        Envoie une phrase à traduire (non-bloquant).
        Le résultat arrive via on_translated(original, translated, src, tgt).

        source_lang : code ISO détecté par Whisper, ou "auto"
        """
        if not self._running:
            return
        if not text or not text.strip():
            return

        # Même langue source = cible → pas besoin de traduire
        effective_src = source_lang if source_lang != "auto" else self._source_lang
        if effective_src != "auto" and effective_src == self._target_lang:
            if self.on_translated:
                self.on_translated(text, text, effective_src, self._target_lang)
            return

        item = {
            "text":   text,
            "source": source_lang,
            "target": self._target_lang,
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            pass  # on abandonne si la queue est pleine

    def translate_sync(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Traduction SYNCHRONE (bloquante) — utile pour la génération de rapport.
        Retourne directement la chaîne traduite.
        """
        if not text or not text.strip():
            return text
        if source_lang == target_lang:
            return text
        return self._do_translate_core(text, source_lang, target_lang)

    # ────────────────────────────────────────────────────────────────────────
    # Worker interne
    # ────────────────────────────────────────────────────────────────────────

    def _worker_loop(self):
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            if not self._ready:
                # Pack pas encore prêt → on remet dans la queue après un délai
                time.sleep(0.5)
                try:
                    self._queue.put_nowait(item)
                except queue.Full:
                    pass
                continue

            translated = self._do_translate_core(
                item["text"], item["source"], item["target"])

            if self.on_translated:
                self.on_translated(
                    item["text"], translated,
                    item["source"], item["target"]
                )

    def _do_translate_core(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Cœur de la traduction :
        1. Protège le glossaire
        2. Vérifie le cache
        3. Tente la traduction directe src → tgt
        4. Fallback : pivot via l'anglais (src → en → tgt)
        5. Restaure le glossaire
        """
        if not ARGOS_AVAILABLE:
            return text

        # Normaliser "auto"
        src = source_lang if source_lang != "auto" else self._source_lang
        tgt = target_lang

        if src == tgt or src == "auto":
            return text

        # Protection glossaire
        protected, mapping = self._protect_glossary(text)

        # Cache
        cache_key = (protected, src, tgt)
        with self._lock:
            if cache_key in self._cache:
                return self._restore_glossary(self._cache[cache_key], mapping)

        # ── Traduction directe ──────────────────────────────────────────────
        result = self._translate_pair(protected, src, tgt)

        # ── Fallback pivot anglais ──────────────────────────────────────────
        if (not result or result == protected) and src != "en" and tgt != "en":
            print(f"[Translation] ⚡ Pivot anglais : {src} → en → {tgt}")
            intermediate = self._translate_pair(protected, src, "en")
            if intermediate and intermediate != protected:
                result = self._translate_pair(intermediate, "en", tgt)

        # ── Fallback pivot français ─────────────────────────────────────────
        if (not result or result == protected) and src != "fr" and tgt != "fr":
            print(f"[Translation] ⚡ Pivot français : {src} → fr → {tgt}")
            intermediate = self._translate_pair(protected, src, "fr")
            if intermediate and intermediate != protected:
                result = self._translate_pair(intermediate, "fr", tgt)

        if not result:
            result = text  # retourner l'original si tout échoue

        # Restaurer glossaire
        result = self._restore_glossary(result, mapping)

        # Mettre en cache
        with self._lock:
            if len(self._cache) > 300:
                self._cache.clear()
            self._cache[cache_key] = result

        return result

    def _translate_pair(self, text: str, src: str, tgt: str) -> str:
        """Tente une traduction directe src → tgt via argostranslate."""
        if not ARGOS_AVAILABLE or src == tgt:
            return text
        try:
            installed = argostranslate.translate.get_installed_languages()
            src_lang  = next((l for l in installed if l.code == src), None)
            tgt_lang  = next((l for l in installed if l.code == tgt), None)
            if not src_lang or not tgt_lang:
                return ""
            translation = src_lang.get_translation(tgt_lang)
            if not translation:
                return ""
            result = translation.translate(text)
            return result if result else ""
        except Exception as e:
            print(f"[Translation] ❌ {src}→{tgt} : {e}")
            return ""

    # ────────────────────────────────────────────────────────────────────────
    # Installation des packs
    # ────────────────────────────────────────────────────────────────────────

    def _ensure_pair_ready(self, source_lang: str, target_lang: str):
        """
        Vérifie que les packs nécessaires pour la paire (source → cible)
        sont installés. Les installe si absent (nécessite internet 1 fois).
        Gère le fallback : si paire directe absente, prépare les packs pivot.
        """
        if not ARGOS_AVAILABLE:
            return

        src = source_lang if source_lang != "auto" else "en"
        tgt = target_lang

        if src == tgt:
            self._ready = True
            return

        try:
            # Paires à vérifier : directe + pivots éventuels
            pairs_needed = [(src, tgt)]
            if src != "en" and tgt != "en":
                pairs_needed += [(src, "en"), ("en", tgt)]
            if src != "fr" and tgt != "fr":
                pairs_needed += [(src, "fr"), ("fr", tgt)]

            # Dédoublonner
            pairs_needed = list(dict.fromkeys(pairs_needed))

            missing = self._find_missing_pairs(pairs_needed)

            if not missing:
                print(f"[Translation] ✅ Packs prêts : {LANG_NAMES.get(src,src)} → {LANG_NAMES.get(tgt,tgt)}")
                self._ready = True
                if self.on_pack_ready:
                    self.on_pack_ready(tgt)
                return

            # Installer les packs manquants
            print(f"[Translation] ⏳ Installation de {len(missing)} pack(s) manquant(s)…")
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()

            for (s, t) in missing:
                pkg = next(
                    (p for p in available if p.from_code == s and p.to_code == t),
                    None
                )
                if pkg:
                    print(f"[Translation] 📦 Installation {LANG_NAMES.get(s,s)} → {LANG_NAMES.get(t,t)}…")
                    argostranslate.package.install_from_path(pkg.download())
                    _installed_pairs[(s, t)] = True
                    print(f"[Translation] ✅ Pack {s}→{t} installé.")
                else:
                    print(f"[Translation] ⚠  Pack {s}→{t} absent de l'index argostranslate.")
                    _installed_pairs[(s, t)] = False

            self._ready = True
            if self.on_pack_ready:
                self.on_pack_ready(tgt)

        except Exception as e:
            print(f"[Translation] ❌ Erreur installation : {e}")
            # Vérifier si on peut quand même fonctionner offline
            self._ready = self._check_any_path_available(src, tgt)
            if self._ready and self.on_pack_ready:
                self.on_pack_ready(tgt)

    def _find_missing_pairs(self, pairs: list) -> list:
        """Retourne les paires de la liste qui ne sont pas encore installées."""
        missing = []
        try:
            installed = argostranslate.translate.get_installed_languages()
            for (src, tgt) in pairs:
                if (src, tgt) in _installed_pairs:
                    continue
                src_lang = next((l for l in installed if l.code == src), None)
                tgt_lang = next((l for l in installed if l.code == tgt), None)
                if src_lang and tgt_lang and src_lang.get_translation(tgt_lang):
                    _installed_pairs[(src, tgt)] = True
                else:
                    missing.append((src, tgt))
        except Exception:
            pass
        return missing

    def _check_any_path_available(self, src: str, tgt: str) -> bool:
        """
        Vérifie si une traduction est possible (directe ou via pivot)
        avec les packs déjà installés, sans internet.
        """
        try:
            installed = argostranslate.translate.get_installed_languages()
            codes = {l.code: l for l in installed}

            # Chemin direct
            if src in codes and tgt in codes:
                if codes[src].get_translation(codes[tgt]):
                    return True

            # Via anglais
            if "en" in codes:
                if (src in codes and codes[src].get_translation(codes["en"]) and
                        codes["en"].get_translation(codes.get(tgt))):
                    return True

            return False
        except Exception:
            return False

    # ────────────────────────────────────────────────────────────────────────
    # Glossaire
    # ────────────────────────────────────────────────────────────────────────

    def _protect_glossary(self, text: str):
        """Remplace les termes du glossaire par des tokens pour les protéger."""
        mapping = {}
        protected = text
        for i, term in enumerate(sorted(self._glossary, key=len, reverse=True)):
            if term.lower() in protected.lower():
                token = f"__TERM{i}__"
                mapping[token] = term
                protected = re.sub(
                    re.escape(term), token, protected, flags=re.IGNORECASE)
        return protected, mapping

    def _restore_glossary(self, text: str, mapping: dict) -> str:
        """Restaure les termes du glossaire dans le texte traduit."""
        for token, term in mapping.items():
            text = text.replace(token, term)
        return text

    def add_glossary_term(self, term: str):
        self._glossary.add(term)
        self._save_glossary()

    def remove_glossary_term(self, term: str):
        self._glossary.discard(term)
        self._save_glossary()

    def get_glossary(self) -> list:
        return sorted(self._glossary)

    def _load_glossary(self):
        try:
            if os.path.isfile(GLOSSARY_FILE):
                with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
                    terms = json.load(f)
                self._glossary.update(terms)
        except Exception:
            pass

    def _save_glossary(self):
        try:
            with open(GLOSSARY_FILE, "w", encoding="utf-8") as f:
                json.dump(sorted(self._glossary), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ────────────────────────────────────────────────────────────────────────
    # Utilitaires publics
    # ────────────────────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def source_language(self) -> str:
        return self._source_lang

    @property
    def target_language(self) -> str:
        return self._target_lang

    @staticmethod
    def get_supported_languages() -> list:
        """Retourne la liste complète des 9 langues supportées."""
        return SUPPORTED_LANGUAGES

    @staticmethod
    def is_argos_installed() -> bool:
        return ARGOS_AVAILABLE

    @staticmethod
    def get_installed_packs() -> list:
        """Retourne les codes des packs argostranslate installés."""
        if not ARGOS_AVAILABLE:
            return []
        try:
            installed = argostranslate.translate.get_installed_languages()
            return [l.code for l in installed]
        except Exception:
            return []

    @staticmethod
    def get_installed_pairs() -> list:
        """Retourne toutes les paires (src, tgt) installées."""
        if not ARGOS_AVAILABLE:
            return []
        try:
            installed = argostranslate.translate.get_installed_languages()
            pairs = []
            for src_lang in installed:
                for tgt_lang in installed:
                    if src_lang.code != tgt_lang.code:
                        if src_lang.get_translation(tgt_lang):
                            pairs.append((src_lang.code, tgt_lang.code))
            return pairs
        except Exception:
            return []