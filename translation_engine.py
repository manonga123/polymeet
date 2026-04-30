"""
translation_engine.py  –  Module F-05/F-06 : Traduction Multilingue Offline
─────────────────────────────────────────────────────────────────────────────
Fonctionnalités :
  • Traduction 100% offline via argos-translate (8 langues)
  • Malagasy 100% offline via Helsinki-NLP/opus-mt-mg-en + opus-mt-en-mg
  • 9 langues : FR, EN, AR, ES, DE, ZH, PT, IT, MG
  • Traduction TOUS AZIMUTS : chaque langue → toutes les autres (72 paires)
  • Détection automatique de la langue source (via Whisper)
  • Fallback via pivot anglais si paire directe absente
  • Chargement des packs en arrière-plan (non-bloquant)
  • Cache des traductions récentes
  • Glossaire personnalisable (termes métier non traduits)

Installation :
  pip install argos-translate transformers sentencepiece sacremoses
  pip install importlib_metadata huggingface_hub
  python install_languages.py
  ← Les modèles Helsinki se téléchargent automatiquement au 1er démarrage
"""

import threading
import queue
import time
import os
import re
import json

# ── Import argos-translate ───────────────────────────────────────────────────
try:
    import argostranslate.package
    import argostranslate.translate
    ARGOS_AVAILABLE = True
except ImportError:
    ARGOS_AVAILABLE = False
    print("[Translation] ⚠  argos-translate non installé → pip install argos-translate")

# ── Import Helsinki-NLP (Malagasy) ───────────────────────────────────────────
try:
    from transformers import MarianMTModel, MarianTokenizer
    HELSINKI_AVAILABLE = True
except ImportError:
    HELSINKI_AVAILABLE = False
    print("[Translation] ⚠  transformers non installé → pip install transformers")

# ─── Langues supportées (toutes les 9) ──────────────────────────────────────
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

ALL_LANG_CODES = [code for code, _, _ in SUPPORTED_LANGUAGES]

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

# Langues gérées par argostranslate
ARGOS_SUPPORTED = {"fr", "en", "ar", "es", "de", "zh", "pt", "it"}

# Modèles Helsinki pour Malagasy
HELSINKI_MG_EN = "Helsinki-NLP/opus-mt-mg-en"
HELSINKI_EN_MG = "Helsinki-NLP/opus-mt-en-mg"

# Glossaire par défaut
DEFAULT_GLOSSARY = {
    "PolyMeet", "Whisper", "API", "CPU", "GPU", "WiFi",
    "DroidCam", "WebSocket", "Python",
}

# Cache paires vérifiées
_installed_pairs: dict = {}

GLOSSARY_FILE = "glossary.json"


# ═══════════════════════════════════════════════════════════════════════════════
# MalagasyEngine — Helsinki-NLP pour MG ↔ EN
# ═══════════════════════════════════════════════════════════════════════════════
class MalagasyEngine:
    """
    Moteur de traduction Malagasy ↔ Anglais via Helsinki-NLP.
    Chargement lazy en arrière-plan pour ne pas bloquer le démarrage.

    Schéma complet Malagasy ↔ autres langues :
      MG → FR  :  MG → EN (Helsinki)  →  EN → FR (Argostranslate)
      FR → MG  :  FR → EN (Argostranslate)  →  EN → MG (Helsinki)
      MG → AR  :  MG → EN (Helsinki)  →  EN → AR (Argostranslate)
      etc.
    """

    def __init__(self):
        self._mg_en_model     = None
        self._mg_en_tokenizer = None
        self._en_mg_model     = None
        self._en_mg_tokenizer = None
        self._lock            = threading.Lock()
        self._mg_en_ready     = False
        self._en_mg_ready     = False

    def load_models(self):
        """Lance le chargement des 2 modèles en arrière-plan."""
        threading.Thread(target=self._load_mg_en, daemon=True,
                         name="Helsinki-MG-EN").start()
        threading.Thread(target=self._load_en_mg, daemon=True,
                         name="Helsinki-EN-MG").start()

    def _load_mg_en(self):
        if not HELSINKI_AVAILABLE:
            return
        try:
            print("[Helsinki] ⏳ Chargement modèle MG→EN…")
            tok   = MarianTokenizer.from_pretrained(HELSINKI_MG_EN)
            model = MarianMTModel.from_pretrained(HELSINKI_MG_EN)
            with self._lock:
                self._mg_en_tokenizer = tok
                self._mg_en_model     = model
                self._mg_en_ready     = True
            print("[Helsinki] ✅ MG→EN prêt !")
        except Exception as e:
            print(f"[Helsinki] ❌ Chargement MG→EN échoué : {e}")

    def _load_en_mg(self):
        if not HELSINKI_AVAILABLE:
            return
        try:
            print("[Helsinki] ⏳ Chargement modèle EN→MG…")
            tok   = MarianTokenizer.from_pretrained(HELSINKI_EN_MG)
            model = MarianMTModel.from_pretrained(HELSINKI_EN_MG)
            with self._lock:
                self._en_mg_tokenizer = tok
                self._en_mg_model     = model
                self._en_mg_ready     = True
            print("[Helsinki] ✅ EN→MG prêt !")
        except Exception as e:
            print(f"[Helsinki] ❌ Chargement EN→MG échoué : {e}")

    def translate_mg_to_en(self, text: str) -> str:
        """Traduit Malagasy → Anglais."""
        with self._lock:
            if not self._mg_en_ready:
                print("[Helsinki] ⚠ MG→EN pas encore chargé, patience…")
                return text
            tok   = self._mg_en_tokenizer
            model = self._mg_en_model
        try:
            inputs  = tok([text], return_tensors="pt",
                          padding=True, truncation=True, max_length=512)
            outputs = model.generate(**inputs, max_length=512)
            return tok.decode(outputs[0], skip_special_tokens=True)
        except Exception as e:
            print(f"[Helsinki] ❌ MG→EN erreur : {e}")
            return text

    def translate_en_to_mg(self, text: str) -> str:
        """Traduit Anglais → Malagasy."""
        with self._lock:
            if not self._en_mg_ready:
                print("[Helsinki] ⚠ EN→MG pas encore chargé, patience…")
                return text
            tok   = self._en_mg_tokenizer
            model = self._en_mg_model
        try:
            inputs  = tok([text], return_tensors="pt",
                          padding=True, truncation=True, max_length=512)
            outputs = model.generate(**inputs, max_length=512)
            return tok.decode(outputs[0], skip_special_tokens=True)
        except Exception as e:
            print(f"[Helsinki] ❌ EN→MG erreur : {e}")
            return text

    @property
    def mg_en_ready(self) -> bool:
        return self._mg_en_ready

    @property
    def en_mg_ready(self) -> bool:
        return self._en_mg_ready


# ═══════════════════════════════════════════════════════════════════════════════
# TranslationEngine principal
# ═══════════════════════════════════════════════════════════════════════════════
class TranslationEngine:
    """
    Moteur de traduction offline complet.

    Routing des traductions :
    ┌─────────────────────────────────────────────────────────────┐
    │  src/tgt sans MG  →  Argostranslate direct                  │
    │                      ou pivot via EN si paire absente       │
    │                                                             │
    │  MG → EN          →  Helsinki opus-mt-mg-en                 │
    │  EN → MG          →  Helsinki opus-mt-en-mg                 │
    │  MG → XX          →  Helsinki MG→EN + Argostranslate EN→XX  │
    │  XX → MG          →  Argostranslate XX→EN + Helsinki EN→MG  │
    └─────────────────────────────────────────────────────────────┘

    Usage :
        engine = TranslationEngine(on_translated=callback)
        engine.start(source_lang="fr", target_lang="mg")
        engine.translate("Bonjour tout le monde", source_lang="fr")
        engine.stop()

    Callback :
        on_translated(original, translated, source_lang, target_lang)
    """

    def __init__(self, on_translated=None, on_pack_ready=None):
        self.on_translated  = on_translated
        self.on_pack_ready  = on_pack_ready

        self._source_lang   = "auto"
        self._target_lang   = "fr"
        self._running       = False
        self._ready         = False
        self._queue         = queue.Queue(maxsize=20)
        self._worker_thread = None
        self._cache         = {}
        self._glossary      = set(DEFAULT_GLOSSARY)
        self._lock          = threading.Lock()

        # Moteur Malagasy Helsinki
        self._malagasy = MalagasyEngine()

        self._load_glossary()

    # ── Démarrage / Arrêt ────────────────────────────────────────────────────

    def start(self, source_lang: str = "auto", target_lang: str = "fr"):
        """Démarre le moteur. Non-bloquant — tout se charge en arrière-plan."""
        if not ARGOS_AVAILABLE:
            print("[Translation] ❌ argos-translate manquant")
            return False

        self._source_lang = source_lang
        self._target_lang = target_lang
        self._running     = True

        # Charger les modèles Helsinki en arrière-plan (toujours, peu importe la langue)
        if HELSINKI_AVAILABLE:
            self._malagasy.load_models()
        else:
            print("[Translation] ⚠ Helsinki non disponible — Malagasy limité")

        # Thread worker de traduction
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="TranslationWorker")
        self._worker_thread.start()

        # Vérifier/installer les packs argostranslate
        threading.Thread(
            target=self._ensure_pair_ready,
            args=(source_lang, target_lang),
            daemon=True, name="PackInstaller"
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

    # ── Gestion des langues ──────────────────────────────────────────────────

    def set_source_language(self, lang_code: str):
        """Définit la langue source (détectée automatiquement par Whisper)."""
        if lang_code == self._source_lang:
            return
        self._source_lang = lang_code
        print(f"[Translation] Langue source → {LANG_NAMES.get(lang_code, lang_code)}")
        if self._target_lang and lang_code != "auto":
            self._ready = False
            threading.Thread(
                target=self._ensure_pair_ready,
                args=(lang_code, self._target_lang),
                daemon=True
            ).start()

    def set_target_language(self, lang_code: str):
        """Change la langue cible à la volée."""
        if lang_code == self._target_lang:
            return
        self._target_lang = lang_code
        self._ready       = False
        print(f"[Translation] Langue cible → {LANG_NAMES.get(lang_code, lang_code)}")
        threading.Thread(
            target=self._ensure_pair_ready,
            args=(self._source_lang, lang_code),
            daemon=True
        ).start()

    def get_available_targets(self, source_lang: str) -> list:
        """Retourne toutes les langues cibles disponibles (toutes sauf la source)."""
        return [
            (code, name, flag)
            for code, name, flag in SUPPORTED_LANGUAGES
            if code != source_lang
        ]

    # ── Traduction publique ──────────────────────────────────────────────────

    def translate(self, text: str, source_lang: str = "auto"):
        """
        Traduction asynchrone (non-bloquante).
        Le résultat arrive via on_translated(original, translated, src, tgt).
        """
        if not self._running or not text or not text.strip():
            return

        effective_src = source_lang if source_lang != "auto" else self._source_lang
        if effective_src != "auto" and effective_src == self._target_lang:
            if self.on_translated:
                self.on_translated(text, text, effective_src, self._target_lang)
            return

        item = {"text": text, "source": source_lang, "target": self._target_lang}
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            pass

    def translate_sync(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Traduction synchrone (bloquante).
        Utile pour la génération de rapports Word/PDF.
        """
        if not text or not text.strip() or source_lang == target_lang:
            return text
        return self._do_translate_core(text, source_lang, target_lang)

    # ── Worker interne ───────────────────────────────────────────────────────

    def _worker_loop(self):
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            if not self._ready:
                # Moteur pas encore prêt → remettre dans la queue
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
                    item["source"], item["target"])

    # ── Cœur de la traduction ────────────────────────────────────────────────

    def _do_translate_core(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Logique complète de traduction avec routing automatique :

        Sans Malagasy :
          src → tgt direct (Argostranslate)
          ou src → EN → tgt (pivot anglais)
          ou src → FR → tgt (pivot français)

        Avec Malagasy :
          MG → EN           : Helsinki direct
          EN → MG           : Helsinki direct
          MG → XX (≠EN)     : Helsinki MG→EN + Argostranslate EN→XX
          XX (≠EN) → MG     : Argostranslate XX→EN + Helsinki EN→MG
        """
        if not ARGOS_AVAILABLE:
            return text

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

        result = ""

        # ── CAS MALAGASY ─────────────────────────────────────────────────
        if src == "mg" and tgt == "en":
            # MG → EN : Helsinki direct
            result = self._helsinki_mg_to_en(protected)

        elif src == "en" and tgt == "mg":
            # EN → MG : Helsinki direct
            result = self._helsinki_en_to_mg(protected)

        elif src == "mg":
            # MG → XX : Helsinki MG→EN puis Argostranslate EN→XX
            en_text = self._helsinki_mg_to_en(protected)
            if en_text and en_text != protected:
                result = self._argos_translate(en_text, "en", tgt)
                if not result:
                    result = en_text  # au moins retourner l'anglais

        elif tgt == "mg":
            # XX → MG : Argostranslate XX→EN puis Helsinki EN→MG
            en_text = self._argos_translate(protected, src, "en")
            if en_text and en_text != protected:
                result = self._helsinki_en_to_mg(en_text)
            elif not en_text:
                # Essayer via français si EN échoue
                fr_text = self._argos_translate(protected, src, "fr")
                if fr_text and fr_text != protected:
                    en_text2 = self._argos_translate(fr_text, "fr", "en")
                    if en_text2:
                        result = self._helsinki_en_to_mg(en_text2)

        # ── CAS STANDARD (sans Malagasy) ─────────────────────────────────
        else:
            # 1. Tentative directe
            result = self._argos_translate(protected, src, tgt)

            # 2. Fallback pivot anglais
            if (not result or result == protected) and src != "en" and tgt != "en":
                print(f"[Translation] ⚡ Pivot EN : {src} → en → {tgt}")
                en_text = self._argos_translate(protected, src, "en")
                if en_text and en_text != protected:
                    result = self._argos_translate(en_text, "en", tgt)

            # 3. Fallback pivot français
            if (not result or result == protected) and src != "fr" and tgt != "fr":
                print(f"[Translation] ⚡ Pivot FR : {src} → fr → {tgt}")
                fr_text = self._argos_translate(protected, src, "fr")
                if fr_text and fr_text != protected:
                    result = self._argos_translate(fr_text, "fr", tgt)

        # Si tout échoue, retourner le texte original
        if not result:
            result = text

        # Restaurer glossaire
        result = self._restore_glossary(result, mapping)

        # Mise en cache
        with self._lock:
            if len(self._cache) > 300:
                self._cache.clear()
            self._cache[cache_key] = result

        return result

    # ── Argostranslate ───────────────────────────────────────────────────────

    def _argos_translate(self, text: str, src: str, tgt: str) -> str:
        """Traduction directe via argostranslate."""
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
            print(f"[Argos] ❌ {src}→{tgt} : {e}")
            return ""

    # ── Helsinki-NLP ─────────────────────────────────────────────────────────

    def _helsinki_mg_to_en(self, text: str) -> str:
        """Malagasy → Anglais via Helsinki."""
        if not HELSINKI_AVAILABLE:
            return ""
        return self._malagasy.translate_mg_to_en(text)

    def _helsinki_en_to_mg(self, text: str) -> str:
        """Anglais → Malagasy via Helsinki."""
        if not HELSINKI_AVAILABLE:
            return ""
        return self._malagasy.translate_en_to_mg(text)

    # ── Installation des packs argostranslate ────────────────────────────────

    def _ensure_pair_ready(self, source_lang: str, target_lang: str):
        """
        Vérifie que les packs argostranslate nécessaires sont installés.
        Pour Malagasy : vérifie juste que Helsinki est disponible.
        """
        if not ARGOS_AVAILABLE:
            return

        src = source_lang if source_lang != "auto" else "en"
        tgt = target_lang

        if src == tgt:
            self._ready = True
            return

        # Malagasy → géré par Helsinki, pas besoin d'argostranslate pour MG↔EN
        if src == "mg" or tgt == "mg":
            if HELSINKI_AVAILABLE:
                print(f"[Translation] ✅ Malagasy géré par Helsinki-NLP")
                self._ready = True
                if self.on_pack_ready:
                    self.on_pack_ready(tgt)
            else:
                print("[Translation] ❌ Helsinki non disponible pour Malagasy")
            return

        # Paires standard : vérifier argostranslate
        pairs_needed = [(src, tgt)]
        if src != "en" and tgt != "en":
            pairs_needed += [(src, "en"), ("en", tgt)]
        if src != "fr" and tgt != "fr":
            pairs_needed += [(src, "fr"), ("fr", tgt)]

        pairs_needed = list(dict.fromkeys(pairs_needed))
        missing = self._find_missing_pairs(pairs_needed)

        if not missing:
            print(f"[Translation] ✅ Packs prêts : "
                  f"{LANG_NAMES.get(src,src)} → {LANG_NAMES.get(tgt,tgt)}")
            self._ready = True
            if self.on_pack_ready:
                self.on_pack_ready(tgt)
            return

        # Installer les packs manquants
        try:
            print(f"[Translation] ⏳ Installation de {len(missing)} pack(s)…")
            argostranslate.package.update_package_index()
            available = argostranslate.package.get_available_packages()
            for (s, t) in missing:
                pkg = next(
                    (p for p in available if p.from_code == s and p.to_code == t),
                    None)
                if pkg:
                    print(f"[Translation] 📦 {LANG_NAMES.get(s,s)} → {LANG_NAMES.get(t,t)}…")
                    argostranslate.package.install_from_path(pkg.download())
                    _installed_pairs[(s, t)] = True
                    print(f"[Translation] ✅ {s}→{t} installé.")
                else:
                    print(f"[Translation] ⚠ {s}→{t} absent de l'index.")
        except Exception as e:
            print(f"[Translation] ❌ Erreur installation : {e}")

        self._ready = True
        if self.on_pack_ready:
            self.on_pack_ready(tgt)

    def _find_missing_pairs(self, pairs: list) -> list:
        """Retourne les paires non encore installées."""
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

    # ── Glossaire ────────────────────────────────────────────────────────────

    def _protect_glossary(self, text: str):
        """Remplace les termes du glossaire par des tokens pour les protéger."""
        mapping   = {}
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

    # ── Utilitaires publics ──────────────────────────────────────────────────

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def source_language(self) -> str:
        return self._source_lang

    @property
    def target_language(self) -> str:
        return self._target_lang

    @property
    def malagasy_ready(self) -> bool:
        """True si les 2 modèles Helsinki sont entièrement chargés."""
        return self._malagasy.mg_en_ready and self._malagasy.en_mg_ready

    @staticmethod
    def get_supported_languages() -> list:
        return SUPPORTED_LANGUAGES

    @staticmethod
    def is_argos_installed() -> bool:
        return ARGOS_AVAILABLE

    @staticmethod
    def is_helsinki_installed() -> bool:
        return HELSINKI_AVAILABLE

    @staticmethod
    def get_installed_packs() -> list:
        """Retourne les codes langues des packs argostranslate installés."""
        if not ARGOS_AVAILABLE:
            return []
        try:
            installed = argostranslate.translate.get_installed_languages()
            return [l.code for l in installed]
        except Exception:
            return []

    @staticmethod
    def get_installed_pairs() -> list:
        """Retourne toutes les paires (src, tgt) installées dans argostranslate."""
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