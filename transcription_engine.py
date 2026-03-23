"""
transcription_engine.py  –  Module F-03 : Transcription Automatique
─────────────────────────────────────────────────────────────────────
Fonctionnalités :
  • Transcription en temps réel via Whisper AI (modèle "small")
  • Latence cible < 2 secondes
  • Détection automatique de la langue parlée
  • Callback vers l'UI à chaque nouveau segment transcrit
  • Buffer glissant : accumule ~3s d'audio avant d'envoyer à Whisper
  • Thread dédié non-bloquant (n'affecte pas l'UI ni l'audio)
  • Correction en direct exposée via get/set du texte complet

Installation :
  pip install openai-whisper
  pip install numpy
  (ffmpeg doit être installé sur le système)
"""

import threading
import queue
import time
import os
import numpy as np
import io
import wave
import datetime

# ── Import Whisper (avec message clair si absent) ────────────────────────────
try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    print("[Transcription] ⚠  openai-whisper non installé.")
    print("                   → pip install openai-whisper")

# ─── Paramètres audio (doivent correspondre à AudioEngine dans client.py) ───
AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_WIDTH    = 2          # paInt16 = 2 bytes

# ─── Paramètres Whisper ──────────────────────────────────────────────────────
WHISPER_MODEL        = "small"      # tiny / base / small / medium / large
BUFFER_SECONDS       = 3.0          # secondes accumulées avant transcription
MIN_AUDIO_SECONDS    = 0.8          # ignorer les buffers trop courts (bruit)
SILENCE_THRESHOLD    = 200          # RMS sous ce seuil = silence
MAX_QUEUE_SIZE       = 20           # anti-saturation de la queue


class TranscriptionEngine:
    """
    Moteur de transcription Whisper en arrière-plan.

    Usage typique dans client.py :
        engine = TranscriptionEngine(on_segment=my_callback)
        engine.start()
        # ... dans AudioEngine._capture_callback :
        engine.feed(raw_bytes)
        # ...
        engine.stop()

    Callback :
        on_segment(text: str, language: str, timestamp: str)
    """

    def __init__(self, on_segment=None):
        """
        on_segment : callable(text, language, timestamp)
            Appelé dans le thread de transcription (pas le thread UI).
            Utiliser root.after(0, ...) dans le callback si mise à jour UI.
        """
        self.on_segment             = on_segment
        self._model                 = None
        self._error                 = ""
        self._queue                 = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._buffer                = bytearray()
        self._lock                  = threading.Lock()
        self._running               = False
        self._thread                = None
        self._full_text             = []
        self._last_lang             = "?"
        # Speaker courant (mis à jour par DiarizationEngine)
        self._current_speaker_label = "?"
        self._current_speaker_color = "#aaaaaa"

    # ── Cycle de vie ────────────────────────────────────────────────────────

    @staticmethod
    def is_model_cached() -> bool:
        """
        Vérifie si le modèle Whisper est déjà téléchargé en local.
        Retourne True  → chargement offline possible.
        Retourne False → internet requis pour le 1er téléchargement.
        """
        if not WHISPER_AVAILABLE:
            return False
        cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "whisper")
        model_file = os.path.join(cache_dir, f"{WHISPER_MODEL}.pt")
        return os.path.isfile(model_file)

    @staticmethod
    def get_cache_path() -> str:
        """Retourne le chemin attendu du fichier modèle en cache."""
        cache_dir = os.path.join(os.path.expanduser("~"), ".cache", "whisper")
        return os.path.join(cache_dir, f"{WHISPER_MODEL}.pt")

    def start(self):
        """
        Charge le modèle Whisper et démarre le thread de transcription.

        Si le modèle n'est pas en cache et qu'internet est absent,
        retourne False avec un message explicite.
        """
        if not WHISPER_AVAILABLE:
            print("[Transcription] ❌ Impossible de démarrer : whisper absent.")
            print("                   → pip install openai-whisper")
            return False
        if self._running:
            return True

        cached = self.is_model_cached()
        if cached:
            print(f"[Transcription] ✅ Modèle '{WHISPER_MODEL}' trouvé en cache — chargement offline.")
        else:
            print(f"[Transcription] ⚠  Modèle '{WHISPER_MODEL}' absent du cache.")
            print(f"                   Tentative de téléchargement (~460 MB)…")
            print(f"                   Cache attendu : {self.get_cache_path()}")

        print(f"[Transcription] ⏳ Chargement du modèle '{WHISPER_MODEL}'…")
        try:
            self._model = whisper.load_model(WHISPER_MODEL)
            print(f"[Transcription] ✅ Modèle '{WHISPER_MODEL}' chargé avec succès.")
        except Exception as e:
            err = str(e)
            if any(kw in err.lower() for kw in ("connection", "network", "timeout",
                                                  "urlopen", "ssl", "certificate")):
                print(f"[Transcription] ❌ Erreur réseau — modèle non téléchargé.")
                print(f"                   Connectez-vous à internet pour le 1er téléchargement.")
                print(f"                   Une fois téléchargé, fonctionne 100% offline.")
                self._error = "no_internet"
            else:
                print(f"[Transcription] ❌ Erreur chargement modèle : {e}")
                self._error = str(e)
            return False

        self._running = True
        self._thread  = threading.Thread(
            target=self._worker_loop, daemon=True, name="WhisperWorker")
        self._thread.start()
        return True

    def stop(self):
        """Arrête le thread proprement."""
        self._running = False
        try:
            self._queue.put_nowait(None)   # poison pill
        except queue.Full:
            pass
        if self._thread:
            self._thread.join(timeout=5)
        self._thread = None
        print("[Transcription] Arrêté.")

    # ── Alimentation en audio ────────────────────────────────────────────────

    def feed(self, raw_bytes: bytes):
        """
        Reçoit un chunk PCM brut depuis AudioEngine.
        Accumule dans le buffer jusqu'à BUFFER_SECONDS puis envoie à Whisper.
        """
        if not self._running:
            return

        # Filtrage VAD rapide : ignorer les chunks silencieux
        if not self._is_loud_enough(raw_bytes):
            return

        with self._lock:
            self._buffer.extend(raw_bytes)
            buffer_secs = len(self._buffer) / (AUDIO_RATE * AUDIO_WIDTH * AUDIO_CHANNELS)

            if buffer_secs >= BUFFER_SECONDS:
                chunk = bytes(self._buffer)
                self._buffer = bytearray()
                try:
                    self._queue.put_nowait(chunk)
                except queue.Full:
                    pass   # on abandonne ce chunk plutôt que bloquer

    # ── Thread de transcription ──────────────────────────────────────────────

    def _worker_loop(self):
        while self._running:
            try:
                chunk = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if chunk is None:       # poison pill
                break

            self._transcribe(chunk)

    def _transcribe(self, raw_bytes: bytes):
        """Transcrit un bloc audio PCM avec Whisper."""
        if self._model is None:
            return

        # Vérification durée minimale
        duration = len(raw_bytes) / (AUDIO_RATE * AUDIO_WIDTH * AUDIO_CHANNELS)
        if duration < MIN_AUDIO_SECONDS:
            return

        try:
            # Conversion PCM → float32 normalisé (format attendu par Whisper)
            audio_np = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            audio_np /= 32768.0

            # Appel Whisper
            result = self._model.transcribe(
                audio_np,
                language=None,          # détection automatique
                fp16=False,             # compatibilité CPU
                task="transcribe",
                verbose=False,
                condition_on_previous_text=True,
                no_speech_threshold=0.6,
                logprob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )

            text = result.get("text", "").strip()
            lang = result.get("language", "?")

            # Ignorer les segments vides ou artefacts Whisper courants
            if not text or text in (".", "...", "Sous-titres réalisés para la communauté d'Amara.org"):
                return

            self._last_lang = lang
            ts = datetime.datetime.now().strftime("%H:%M:%S")

            # Stocker dans l'historique
            # Stocker dans l'historique (avec speaker)
            self._full_text.append({
                "time":          ts,
                "lang":          lang,
                "text":          text,
                "speaker_label": self._current_speaker_label,
                "speaker_color": self._current_speaker_color,
            })

            # Callback vers l'UI
            if self.on_segment:
                self.on_segment(
                    text=text,
                    language=lang,
                    timestamp=ts,
                    speaker_label=self._current_speaker_label,
                    speaker_color=self._current_speaker_color,
                )

        except Exception as e:
            print(f"[Transcription] ❌ Erreur Whisper : {e}")

    # ── Utilitaires ──────────────────────────────────────────────────────────

    @staticmethod
    def _is_loud_enough(raw_bytes: bytes) -> bool:
        """Retourne True si le chunk audio dépasse le seuil RMS de silence."""
        try:
            samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples ** 2)))
            return rms > SILENCE_THRESHOLD
        except Exception:
            return False

    def set_current_speaker(self, label: str, color: str):
        """Mis à jour par DiarizationEngine quand le speaker courant change."""
        self._current_speaker_label = label
        self._current_speaker_color = color

    def get_full_transcript(self) -> str:
        """Retourne la transcription complète avec noms des speakers."""
        lines = []
        for entry in self._full_text:
            speaker = entry.get("speaker_label", "?")
            lines.append(
                f"[{entry['time']}] ({entry['lang'].upper()})  "
                f"{speaker}: {entry['text']}")
        return "\n".join(lines)

    def clear_transcript(self):
        """Efface l'historique (utile pour nouvelle réunion)."""
        self._full_text.clear()

    @property
    def last_language(self) -> str:
        return self._last_lang

    @property
    def is_running(self) -> bool:
        return self._running