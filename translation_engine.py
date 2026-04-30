"""
translation_engine.py  –  Module F-05/F-06 : Traduction Multilingue Offline
─────────────────────────────────────────────────────────────────────────────
Fonctionnalités :
  • Traduction 100% offline via argos-translate
  • 9 langues : FR, EN, AR, ES, DE, ZH, PT, IT + MG (fallback)
  • Chargement des packs de langue en arrière-plan (non-bloquant)
  • Cache des traductions récentes (évite de re-traduire la même phrase)
  • Callback vers l'UI dès qu'une traduction est prête
  • Glossaire personnalisable (termes métier non traduits)

Installation :
  pip install argos-translate

Les packs de langue (~100 MB chacun) se téléchargent automatiquement
la première fois via install_language_pack().
"""

import threading
import queue
import time
import os
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

# ─── Langues supportées ──────────────────────────────────────────────────────
# (code_iso, label_affichage, flag)
SUPPORTED_LANGUAGES = [
    ("fr", "Français",   "🇫🇷"),
    ("en", "English",    "🇬🇧"),
    ("mg", "Malagasy",   "🇲🇬"),   # fallback : texte original si non supporté
    ("ar", "Arabe",      "🇸🇦"),
    ("es", "Espagnol",   "🇪🇸"),
    ("de", "Allemand",   "🇩🇪"),
    ("zh", "Chinois",    "🇨🇳"),
    ("pt", "Portugais",  "🇵🇹"),
    ("it", "Italien",    "🇮🇹"),
]

# Langues supportées par argos-translate (Malagasy absent)
ARGOS_SUPPORTED = {"fr", "en", "ar", "es", "de", "zh", "pt", "it"}

# Glossaire par défaut (termes qui ne doivent pas être traduits)
DEFAULT_GLOSSARY = {
    "PolyMeet", "Whisper", "API", "CPU", "GPU", "WiFi",
    "DroidCam", "WebSocket", "Python",
}

# Cache des paires déjà installées : {(src, tgt): bool}
_installed_pairs: dict = {}

# Fichier glossaire personnalisé
GLOSSARY_FILE = "glossary.json"


class TranslationEngine:
    """
    Moteur de traduction offline argos-translate.

    Usage :
        engine = TranslationEngine(on_translated=my_callback)
        engine.set_target_language("fr")
        engine.start()
        engine.translate("Hello world", source_lang="en")
        engine.stop()

    Callback :
        on_translated(original, translated, source_lang, target_lang)
    """

    def __init__(self, on_translated=None, on_pack_ready=None):
        """
        on_translated(original, translated, source_lang, target_lang)
        on_pack_ready(lang_code)  : appelé quand un pack est installé
        """
        self.on_translated  = on_translated
        self.on_pack_ready  = on_pack_ready

        self._target_lang   = "fr"       # langue cible par défaut
        self._running       = False
        self._ready         = False
        self._queue         = queue.Queue(maxsize=10)
        self._worker_thread = None
        self._cache         = {}         # {(text, src, tgt): translated}
        self._glossary      = set(DEFAULT_GLOSSARY)
        self._lock          = threading.Lock()

        self._load_glossary()

    # ── Démarrage ────────────────────────────────────────────────────────────

    def start(self, target_lang: str = "fr"):
        """
        Démarre le moteur. Vérifie/installe les packs nécessaires
        en arrière-plan. L'UI ne se bloque PAS.
        """
        if not ARGOS_AVAILABLE:
            print("[Translation] ❌ argos-translate manquant")
            return False
        self._target_lang = target_lang
        self._running     = True

        # Thread worker de traduction
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="TranslationWorker")
        self._worker_thread.start()

        # Vérifier/installer le pack en arrière-plan
        threading.Thread(
            target=self._ensure_pack,
            args=(target_lang,),
            daemon=True,
            name="PackInstaller"
        ).start()

        print(f"[Translation] ▶ Démarré → cible : {target_lang}")
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

    # ── Langue cible ─────────────────────────────────────────────────────────

    def set_target_language(self, lang_code: str):
        """Change la langue cible à la volée."""
        if lang_code == self._target_lang:
            return
        self._target_lang = lang_code
        self._ready       = False
        print(f"[Translation] Changement langue cible → {lang_code}")

        if lang_code == "mg":
            # Malagasy non supporté par argos → mode passthrough
            self._ready = True
            if self.on_pack_ready:
                self.on_pack_ready("mg")
            return

        # Installer le pack si nécessaire
        threading.Thread(
            target=self._ensure_pack,
            args=(lang_code,),
            daemon=True
        ).start()

    # ── Traduction ───────────────────────────────────────────────────────────

    def translate(self, text: str, source_lang: str = "auto"):
        """
        Envoie une phrase à traduire (non-bloquant).
        Le résultat arrive via on_translated().
        """
        if not self._running:
            return
        if not text or not text.strip():
            return

        # Malagasy : passthrough
        if self._target_lang == "mg":
            if self.on_translated:
                self.on_translated(text, text, source_lang, "mg")
            return

        # Même langue source = cible : pas besoin de traduire
        if source_lang != "auto" and source_lang == self._target_lang:
            if self.on_translated:
                self.on_translated(text, text, source_lang, self._target_lang)
            return

        item = {"text": text, "source": source_lang, "target": self._target_lang}
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            pass   # on abandonne si la queue est pleine

    # ── Worker ───────────────────────────────────────────────────────────────

    def _worker_loop(self):
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            if not self._ready:
                continue   # pack pas encore prêt, on ignore

            self._do_translate(item["text"], item["source"], item["target"])

    def _do_translate(self, text: str, source_lang: str, target_lang: str):
        """Effectue la traduction réelle."""

        # Protection glossaire : remplacer les termes par des tokens
        protected, mapping = self._protect_glossary(text)

        # Cache
        cache_key = (protected, source_lang, target_lang)
        with self._lock:
            if cache_key in self._cache:
                translated = self._cache[cache_key]
                translated = self._restore_glossary(translated, mapping)
                if self.on_translated:
                    self.on_translated(text, translated, source_lang, target_lang)
                return

        try:
            if source_lang == "auto" or source_lang not in ARGOS_SUPPORTED:
                # Détection automatique : essayer depuis "en" et "fr"
                translated = self._translate_with_fallback(
                    protected, target_lang)
            else:
                translated = self._translate_pair(
                    protected, source_lang, target_lang)

            if not translated:
                translated = text

            # Restaurer le glossaire
            translated = self._restore_glossary(translated, mapping)

            # Mettre en cache
            with self._lock:
                if len(self._cache) > 200:
                    # Nettoyer le cache si trop grand
                    self._cache.clear()
                self._cache[cache_key] = translated

            if self.on_translated:
                self.on_translated(text, translated, source_lang, target_lang)

        except Exception as e:
            print(f"[Translation] ❌ Erreur : {e}")
            if self.on_translated:
                self.on_translated(text, text, source_lang, target_lang)

    def _translate_pair(self, text: str, src: str, tgt: str) -> str:
        """Traduit directement d'une langue vers une autre."""
        if not ARGOS_AVAILABLE:
            return text
        try:
            installed = argostranslate.translate.get_installed_languages()
            src_lang  = next((l for l in installed if l.code == src), None)
            tgt_lang  = next((l for l in installed if l.code == tgt), None)
            if not src_lang or not tgt_lang:
                return text
            translation = src_lang.get_translation(tgt_lang)
            if not translation:
                return text
            return translation.translate(text)
        except Exception as e:
            print(f"[Translation] ❌ pair {src}→{tgt} : {e}")
            return text

    def _translate_with_fallback(self, text: str, tgt: str) -> str:
        """
        Détection auto de la langue source :
        essaie EN→tgt puis FR→tgt.
        """
        for src in ("en", "fr", "es", "de", "pt", "it", "ar", "zh"):
            if src == tgt:
                continue
            result = self._translate_pair(text, src, tgt)
            if result and result != text:
                return result
        return text

    # ── Installation des packs ────────────────────────────────────────────────

    def _ensure_pack(self, target_lang: str):
        """
        Vérifie si les packs nécessaires sont installés.
        Les installe automatiquement si absent (nécessite internet 1 fois).
        """
        if target_lang == "mg":
            self._ready = True
            if self.on_pack_ready:
                self.on_pack_ready("mg")
            return

        if target_lang not in ARGOS_SUPPORTED:
            print(f"[Translation] ⚠ Langue '{target_lang}' non supportée par argos")
            return

        try:
            installed = argostranslate.translate.get_installed_languages()
            installed_codes = {l.code for l in installed}

            needs_install = []

            # On a besoin de EN→target et EN→EN (pivot)
            for src in ["en", "fr"]:
                pair_key = (src, target_lang)
                if pair_key in _installed_pairs:
                    continue
                # Vérifier si déjà installé
                src_lang = next((l for l in installed if l.code == src), None)
                tgt_lang = next((l for l in installed if l.code == target_lang), None)
                if src_lang and tgt_lang and src_lang.get_translation(tgt_lang):
                    _installed_pairs[pair_key] = True
                else:
                    needs_install.append(pair_key)

            if not needs_install:
                print(f"[Translation] ✅ Packs disponibles pour '{target_lang}'")
                self._ready = True
                if self.on_pack_ready:
                    self.on_pack_ready(target_lang)
                return

            # Télécharger les packs manquants
            print(f"[Translation] ⏳ Téléchargement packs pour '{target_lang}'…")
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()

            for (src, tgt) in needs_install:
                pkg = next(
                    (p for p in available
                     if p.from_code == src and p.to_code == tgt), None)
                if pkg:
                    print(f"[Translation] 📦 Installation {src}→{tgt}…")
                    argostranslate.package.install_from_path(pkg.download())
                    _installed_pairs[(src, tgt)] = True
                    print(f"[Translation] ✅ Pack {src}→{tgt} installé.")
                else:
                    print(f"[Translation] ⚠ Pack {src}→{tgt} non trouvé.")

            self._ready = True
            if self.on_pack_ready:
                self.on_pack_ready(target_lang)

        except Exception as e:
            print(f"[Translation] ❌ Erreur installation pack : {e}")
            # Si offline et pack déjà installé, on peut quand même fonctionner
            self._ready = self._check_pack_available(target_lang)
            if self._ready and self.on_pack_ready:
                self.on_pack_ready(target_lang)

    def _check_pack_available(self, target_lang: str) -> bool:
        """Vérifie si le pack est disponible sans internet."""
        try:
            installed = argostranslate.translate.get_installed_languages()
            tgt = next((l for l in installed if l.code == target_lang), None)
            if not tgt:
                return False
            for l in installed:
                if l.code != target_lang and l.get_translation(tgt):
                    return True
            return False
        except Exception:
            return False

    # ── Glossaire ─────────────────────────────────────────────────────────────

    def _protect_glossary(self, text: str):
        """Remplace les termes du glossaire par des tokens pour les protéger."""
        mapping = {}
        protected = text
        for i, term in enumerate(self._glossary):
            if term.lower() in text.lower():
                token = f"__TERM{i}__"
                mapping[token] = term
                # Remplacement insensible à la casse
                import re
                protected = re.sub(re.escape(term), token, protected, flags=re.IGNORECASE)
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

    # ── Utilitaires ──────────────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def target_language(self) -> str:
        return self._target_lang

    @staticmethod
    def get_supported_languages() -> list:
        return SUPPORTED_LANGUAGES

    @staticmethod
    def is_argos_installed() -> bool:
        return ARGOS_AVAILABLE

    @staticmethod
    def get_installed_packs() -> list:
        """Retourne la liste des packs argos déjà installés."""
        if not ARGOS_AVAILABLE:
            return []
        try:
            installed = argostranslate.translate.get_installed_languages()
            return [l.code for l in installed]
        except Exception:
            return []