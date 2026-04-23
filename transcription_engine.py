"""
transcription_engine.py  –  Module F-03 : Transcription Automatique (OPTIMISÉ v2)
────────────────────────────────────────────────────────────────────────────────
Optimisations v2 :
  • Modèle "tiny" au lieu de "small" → 5x plus rapide sur CPU
  • Chargement en arrière-plan (ne bloque PAS l'UI)
  • VAD intelligent : envoie dès qu'une pause est détectée (pas d'attente fixe)
  • Queue limitée à 2 items : jamais de retard accumulé
  • Normalisation audio automatique : corrige le micro faible DroidCam
  • beam_size=1, best_of=1, temperature=0 : mode greedy, le plus rapide
  • condition_on_previous_text=False : évite les hallucinations longues
"""

import threading
import queue
import os
import numpy as np
import datetime

try:
    import whisper
    WHISPER_AVAILABLE = True
except ImportError:
    WHISPER_AVAILABLE = False
    print("[Transcription] ⚠  openai-whisper non installé → pip install openai-whisper")

# ─── Paramètres audio ────────────────────────────────────────────────────────
AUDIO_RATE     = 16000
AUDIO_CHANNELS = 1
AUDIO_WIDTH    = 2       # paInt16 = 2 bytes

# ─── Paramètres Whisper ──────────────────────────────────────────────────────
# "tiny"  : ~39 MB, ~1-3s sur CPU   ← RECOMMANDÉ sans GPU
# "base"  : ~74 MB, ~3-6s sur CPU
# "small" : ~244 MB, ~15-30s sur CPU ← trop lent sans GPU
WHISPER_MODEL     = "tiny"

BUFFER_SECONDS    = 4.0   # max avant envoi forcé
SILENCE_TRIGGER   = 6     # chunks de silence avant envoi (≈0.8s)
MAX_QUEUE_SIZE    = 2     # 2 max → pas de retard accumulé
MIN_RMS           = 150   # seuil VAD micro faible
MIN_DURATION      = 0.5   # ignorer les clips < 0.5s

# Hallucinations connues de Whisper à filtrer
HALLUCINATIONS = {
    ".", "..", "...", "merci", "merci.", "thank you.", "thanks.",
    "transcribed by https://otter.ai",
    "sous-titres réalisés para la communauté d'amara.org",
    "you", "you.", "the", "a",
}


class TranscriptionEngine:
    """
    Moteur de transcription Whisper non-bloquant.
    Chargement en arrière-plan + VAD intelligent + normalisation volume.
    """

    def __init__(self, on_segment=None, on_ready=None):
        """
        on_segment(text, language, timestamp, speaker_label, speaker_color)
        on_ready() : appelé depuis le thread loader quand le modèle est prêt
        """
        self.on_segment             = on_segment
        self.on_ready               = on_ready
        self._model                 = None
        self._error                 = ""
        self._queue                 = queue.Queue(maxsize=MAX_QUEUE_SIZE)
        self._lock                  = threading.Lock()
        self._running               = False
        self._ready                 = False
        self._worker_thread         = None
        self._loader_thread         = None
        self._full_text             = []
        self._last_lang             = "?"
        self._current_speaker_label = "?"
        self._current_speaker_color = "#aaaaaa"
        # VAD
        self._vad_buffer     = bytearray()
        self._silence_chunks = 0

    # ── Cache ────────────────────────────────────────────────────────────────

    @staticmethod
    def is_model_cached(model: str = WHISPER_MODEL) -> bool:
        if not WHISPER_AVAILABLE:
            return False
        path = os.path.join(os.path.expanduser("~"), ".cache", "whisper", f"{model}.pt")
        return os.path.isfile(path)

    @staticmethod
    def get_cache_path(model: str = WHISPER_MODEL) -> str:
        return os.path.join(os.path.expanduser("~"), ".cache", "whisper", f"{model}.pt")

    # ── Démarrage non-bloquant ───────────────────────────────────────────────

    def start(self):
        """
        Lance le chargement du modèle dans un thread séparé.
        Retourne True immédiatement — l'UI ne se bloque PAS.
        on_ready() sera appelé quand le modèle est prêt.
        """
        if not WHISPER_AVAILABLE:
            self._error = "whisper_absent"
            return False
        if self._running:
            return True
        self._running = True
        self._loader_thread = threading.Thread(
            target=self._load_model, daemon=True, name="WhisperLoader")
        self._loader_thread.start()
        return True

    def _load_model(self):
        cached = self.is_model_cached()
        print(f"[Transcription] ⏳ Chargement '{WHISPER_MODEL}' "
              f"({'depuis cache' if cached else 'téléchargement…'})")
        try:
            self._model = whisper.load_model(WHISPER_MODEL)
            self._ready = True
            print(f"[Transcription] ✅ Modèle '{WHISPER_MODEL}' prêt.")
            # Démarrer le worker
            self._worker_thread = threading.Thread(
                target=self._worker_loop, daemon=True, name="WhisperWorker")
            self._worker_thread.start()
            if self.on_ready:
                self.on_ready()
        except Exception as e:
            self._running = False
            err = str(e).lower()
            if any(k in err for k in ("connection", "network", "timeout", "urlopen", "ssl")):
                self._error = "no_internet"
            else:
                self._error = str(e)
            print(f"[Transcription] ❌ Chargement échoué : {e}")

    def stop(self):
        self._running = False
        self._ready   = False
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self._worker_thread:
            self._worker_thread.join(timeout=3)
            self._worker_thread = None
        print("[Transcription] Arrêté.")

    # ── Alimentation audio ───────────────────────────────────────────────────

    def feed(self, raw_bytes: bytes):
        """
        Reçoit un chunk PCM 16000 Hz.
        VAD : accumule la parole, envoie dès qu'une pause est détectée.
        """
        if not self._running or not self._ready:
            return

        rms = self._rms(raw_bytes)
        is_speech = rms > MIN_RMS

        with self._lock:
            if is_speech:
                self._silence_chunks = 0
                self._vad_buffer.extend(raw_bytes)
            else:
                if len(self._vad_buffer) > 0:
                    self._silence_chunks += 1
                    self._vad_buffer.extend(raw_bytes)  # contexte silence

            buf_secs = len(self._vad_buffer) / (AUDIO_RATE * AUDIO_WIDTH)

            send = (
                (self._silence_chunks >= SILENCE_TRIGGER and buf_secs >= MIN_DURATION)
                or buf_secs >= BUFFER_SECONDS
            )

            if send:
                chunk = bytes(self._vad_buffer)
                self._vad_buffer     = bytearray()
                self._silence_chunks = 0
                if not self._queue.full():
                    self._queue.put_nowait(chunk)
                # Si queue pleine : on abandonne ce chunk (évite l'accumulation)

    # ── Worker ───────────────────────────────────────────────────────────────

    def _worker_loop(self):
        while self._running:
            try:
                chunk = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if chunk is None:
                break
            self._transcribe(chunk)

    def _transcribe(self, raw_bytes: bytes):
        if not self._model:
            return
        duration = len(raw_bytes) / (AUDIO_RATE * AUDIO_WIDTH)
        if duration < MIN_DURATION:
            return
        try:
            # PCM → float32 normalisé
            audio = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            audio /= 32768.0

            # Normalisation volume (corrige micro DroidCam très faible)
            peak = float(np.max(np.abs(audio)))
            if 0.001 < peak < 0.15:
                audio = np.clip(audio * (0.8 / peak), -1.0, 1.0)
            elif 0.0 < peak < 1.0:
                audio = audio / peak * 0.9

            # Transcription optimisée CPU
            result = self._model.transcribe(
                audio,
                language=None,                    # détection auto langue
                fp16=False,                       # CPU uniquement
                task="transcribe",
                verbose=False,
                beam_size=1,                      # greedy = le plus rapide
                best_of=1,
                temperature=0.0,                  # déterministe
                condition_on_previous_text=False, # pas d'hallucinations
                no_speech_threshold=0.5,
                logprob_threshold=-1.0,
                compression_ratio_threshold=2.4,
            )

            text = result.get("text", "").strip()
            lang = result.get("language", "?")

            # Filtrer hallucinations et textes trop courts
            if not text or len(text) < 3:
                return
            if text.lower().rstrip(".!? ") in HALLUCINATIONS:
                return

            self._last_lang = lang
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            print(f"[Transcription] [{lang.upper()}] {text[:60]}")

            self._full_text.append({
                "time": ts, "lang": lang, "text": text,
                "speaker_label": self._current_speaker_label,
                "speaker_color": self._current_speaker_color,
            })

            if self.on_segment:
                self.on_segment(
                    text=text, language=lang, timestamp=ts,
                    speaker_label=self._current_speaker_label,
                    speaker_color=self._current_speaker_color,
                )

        except Exception as e:
            print(f"[Transcription] ❌ {e}")

    # ── Utilitaires ──────────────────────────────────────────────────────────

    @staticmethod
    def _rms(raw_bytes: bytes) -> float:
        try:
            s = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            return float(np.sqrt(np.mean(s ** 2))) if len(s) > 0 else 0.0
        except Exception:
            return 0.0

    def set_current_speaker(self, label: str, color: str):
        self._current_speaker_label = label
        self._current_speaker_color = color

    def get_full_transcript(self) -> str:
        lines = []
        for e in self._full_text:
            lines.append(f"[{e['time']}] ({e['lang'].upper()})  "
                         f"{e.get('speaker_label','?')}: {e['text']}")
        return "\n".join(lines)

    def clear_transcript(self):
        self._full_text.clear()

    @property
    def last_language(self) -> str:
        return self._last_lang

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_ready(self) -> bool:
        return self._ready
