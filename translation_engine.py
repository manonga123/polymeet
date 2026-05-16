"""
translation_engine.py  –  Module F-05/F-06 : Traduction via Gemini 2.5 Flash
─────────────────────────────────────────────────────────────────────────────
Fonctionnalités :
  • Traduction via Google Gemini 2.5 Flash (API en ligne)
  • 9 langues : FR, EN, AR, ES, DE, ZH, PT, IT, MG
  • Détection automatique de la langue source (depuis Deepgram)
  • Traduction UNIQUEMENT des phrases de la transcription (pas d'écoute audio)
  • Cache des traductions récentes pour éviter les doublons
  • Worker asynchrone non-bloquant

Installation :
  pip install google-generativeai
"""

import threading
import queue
import time
import os

# ── Import Google Generative AI ──────────────────────────────────────────────
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False
    print("[Translation] ⚠  google-generativeai non installé → pip install google-generativeai")

# ── Clé API Gemini ───────────────────────────────────────────────────────────
# Remplacez par votre clé API Google AI Studio : https://aistudio.google.com/app/apikey
GEMINI_API_KEY = "AQ.Ab8RN6IyrC5Na8n8_8IgjdGN3_vpa2eoOBA5m7INkgTUUm7ZLg"

# ── Langues supportées ───────────────────────────────────────────────────────
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

# Noms complets pour Gemini (plus explicite que les codes ISO)
LANG_FULL_NAMES = {
    "fr": "French",
    "en": "English",
    "ar": "Arabic",
    "es": "Spanish",
    "de": "German",
    "zh": "Chinese (Simplified)",
    "pt": "Portuguese",
    "it": "Italian",
    "mg": "Malagasy",
}


# ═══════════════════════════════════════════════════════════════════════════════
# TranslationEngine — Gemini 2.5 Flash
# ═══════════════════════════════════════════════════════════════════════════════
class TranslationEngine:
    """
    Moteur de traduction via Google Gemini 2.5 Flash.

    PRINCIPE :
      - On ne capte PAS l'audio — on traduit uniquement les segments
        déjà transcrits par Deepgram.
      - Le client appelle translate(text, source_lang) à chaque segment.
      - Le résultat arrive via le callback on_translated(original, translated, src, tgt).

    Usage :
        engine = TranslationEngine(on_translated=callback)
        engine.start(target_lang="fr")
        engine.translate("You're such a good kid", source_lang="en")
        engine.stop()
    """

    def __init__(self, on_translated=None, on_pack_ready=None):
        self.on_translated  = on_translated
        self.on_pack_ready  = on_pack_ready  # gardé pour compatibilité API

        self._target_lang   = "fr"
        self._source_lang   = "auto"
        self._running       = False
        self._ready         = False
        self._queue         = queue.Queue(maxsize=50)
        self._worker_thread = None
        self._cache         = {}
        self._lock          = threading.Lock()

        # Modèle Gemini
        self._model         = None

    # ── Démarrage / Arrêt ────────────────────────────────────────────────────

    def start(self, source_lang: str = "auto", target_lang: str = "fr") -> bool:
        """Démarre le moteur. Non-bloquant."""
        if not GEMINI_AVAILABLE:
            print("[Translation] ❌ google-generativeai non installé")
            return False

        self._source_lang = source_lang
        self._target_lang = target_lang
        self._running     = True

        # Initialiser Gemini dans un thread pour ne pas bloquer l'UI
        threading.Thread(target=self._init_gemini, daemon=True,
                         name="GeminiInit").start()

        # Thread worker de traduction
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="TranslationWorker")
        self._worker_thread.start()

        tgt_name = LANG_NAMES.get(target_lang, target_lang)
        print(f"[Translation] ▶ Démarré → {tgt_name} (Gemini 2.5 Flash)")
        return True

    def stop(self):
        """Arrête le moteur proprement."""
        self._running = False
        self._ready   = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
        print("[Translation] Arrêté.")

    # ── Initialisation Gemini ────────────────────────────────────────────────

    def _init_gemini(self):
        """Configure le client Gemini (non-bloquant)."""
        try:
            api_key = GEMINI_API_KEY
            # Essayer aussi depuis variable d'environnement
            env_key = os.environ.get("GEMINI_API_KEY", "")
            if env_key:
                api_key = env_key

            if api_key == "VOTRE_CLE_API_GEMINI_ICI" or not api_key:
                print("[Translation] ❌ Clé API Gemini non configurée !")
                print("             Définissez GEMINI_API_KEY dans translation_engine.py")
                print("             ou via la variable d'environnement GEMINI_API_KEY")
                return

            genai.configure(api_key=api_key)
            self._model = genai.GenerativeModel("gemini-2.5-flash")
            self._ready = True

            tgt_name = LANG_NAMES.get(self._target_lang, self._target_lang)
            print(f"[Translation] ✅ Gemini 2.5 Flash prêt → {tgt_name}")

            if self.on_pack_ready:
                self.on_pack_ready(self._target_lang)

        except Exception as e:
            print(f"[Translation] ❌ Erreur init Gemini : {e}")

    # ── Gestion des langues ──────────────────────────────────────────────────

    def set_target_language(self, lang_code: str):
        """Change la langue cible à la volée."""
        if lang_code == self._target_lang:
            return
        self._target_lang = lang_code
        # Vider le cache car la langue a changé
        with self._lock:
            self._cache.clear()
        tgt_name = LANG_NAMES.get(lang_code, lang_code)
        print(f"[Translation] Langue cible → {tgt_name}")

    def set_source_language(self, lang_code: str):
        """Met à jour la langue source (fournie par Deepgram)."""
        if lang_code != self._source_lang:
            self._source_lang = lang_code

    def get_available_targets(self, source_lang: str) -> list:
        """Retourne toutes les langues cibles disponibles."""
        return [
            (code, name, flag)
            for code, name, flag in SUPPORTED_LANGUAGES
            if code != source_lang
        ]

    # ── Traduction publique ──────────────────────────────────────────────────

    def translate(self, text: str, source_lang: str = "auto"):
        """
        Traduction asynchrone (non-bloquante).
        Appelée par client.py à chaque segment de transcription.
        Résultat via callback on_translated(original, translated, src, tgt).
        """
        if not self._running or not text or not text.strip():
            return

        effective_src = source_lang if source_lang not in ("auto", "?", None) else self._source_lang

        # Si même langue : pas besoin de traduire
        if effective_src != "auto" and effective_src == self._target_lang:
            if self.on_translated:
                self.on_translated(text, text, effective_src, self._target_lang)
            return

        item = {
            "text": text,
            "source": effective_src,
            "target": self._target_lang,
        }
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            print("[Translation] ⚠ Queue pleine, segment ignoré")

    def translate_sync(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Traduction synchrone (bloquante) — pour rapports Word/PDF.
        """
        if not text or not text.strip() or source_lang == target_lang:
            return text
        return self._do_translate(text, source_lang, target_lang)

    # ── Worker interne ───────────────────────────────────────────────────────

    def _worker_loop(self):
        """Thread worker : consomme la queue et appelle Gemini."""
        while self._running:
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if item is None:
                break

            # Attendre que Gemini soit prêt (max 30s)
            wait_start = time.time()
            while not self._ready and self._running:
                if time.time() - wait_start > 30:
                    print("[Translation] ⚠ Timeout attente Gemini")
                    break
                time.sleep(0.3)

            if not self._ready:
                continue

            translated = self._do_translate(
                item["text"], item["source"], item["target"])

            if self.on_translated and translated:
                self.on_translated(
                    item["text"],
                    translated,
                    item["source"],
                    item["target"],
                )

    # ── Cœur de la traduction (Gemini) ───────────────────────────────────────

    def _do_translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """
        Appelle Gemini 2.5 Flash pour traduire le texte.
        Utilise un cache pour éviter de re-traduire les phrases identiques.
        """
        if not text or not text.strip():
            return text

        src = source_lang if source_lang not in ("auto", "?", None) else "auto"
        tgt = target_lang

        # Cache
        cache_key = (text.strip(), src, tgt)
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        try:
            tgt_full = LANG_FULL_NAMES.get(tgt, tgt)

            if src == "auto":
                prompt = (
                    f"Translate the following text to {tgt_full}. "
                    f"Return ONLY the translated text, nothing else, no explanation:\n\n{text}"
                )
            else:
                src_full = LANG_FULL_NAMES.get(src, src)
                prompt = (
                    f"Translate the following {src_full} text to {tgt_full}. "
                    f"Return ONLY the translated text, nothing else, no explanation:\n\n{text}"
                )

            response = self._model.generate_content(prompt)
            result = response.text.strip()

            # Mise en cache (limite 500 entrées)
            with self._lock:
                if len(self._cache) > 500:
                    # Supprimer la moitié la plus ancienne
                    keys = list(self._cache.keys())
                    for k in keys[:250]:
                        del self._cache[k]
                self._cache[cache_key] = result

            return result

        except Exception as e:
            print(f"[Translation] ❌ Erreur Gemini : {e}")
            return text  # retourner l'original en cas d'échec

    # ── Propriétés publiques ─────────────────────────────────────────────────

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
        return SUPPORTED_LANGUAGES

    @staticmethod
    def is_argos_installed() -> bool:
        """Compatibilité — toujours False, on utilise Gemini."""
        return False

    @staticmethod
    def is_helsinki_installed() -> bool:
        """Compatibilité — toujours False, on utilise Gemini."""
        return False

    @staticmethod
    def get_installed_packs() -> list:
        """Compatibilité — retourne toutes les langues (Gemini supporte tout)."""
        return [code for code, _, _ in SUPPORTED_LANGUAGES]

    @staticmethod
    def get_installed_pairs() -> list:
        """Compatibilité — Gemini supporte toutes les paires."""
        pairs = []
        codes = [c for c, _, _ in SUPPORTED_LANGUAGES]
        for src in codes:
            for tgt in codes:
                if src != tgt:
                    pairs.append((src, tgt))
        return pairs