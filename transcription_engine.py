import threading
import datetime
import time
import numpy as np

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveTranscriptionEvents,
    LiveOptions,
)

API_KEY = "d8163d02f362e0d375d64a3dad14841d764fc1ab"

DEEPGRAM_SAMPLE_RATE = 16000


class TranscriptionEngine:
    def __init__(self, on_transcript_segment):
        """
        on_transcript_segment(text, language, timestamp, speaker_label, speaker_color)
        """
        self.on_transcript_segment = on_transcript_segment
        self.is_running   = False
        self._connection  = None
        self._thread      = None
        self._full_text   = []
        self._last_lang   = "?"
        self._cur_speaker = "?"
        self._cur_color   = "#aaaaaa"

        # Buffer audio
        self._audio_buffer    = bytearray()
        self._buffer_lock     = threading.Lock()
        self._MIN_CHUNK_BYTES = 3200   # ~100ms à 16kHz 16bit mono
        self._flush_timer     = None

        # Client Deepgram
        config = DeepgramClientOptions(verbose=False)
        self._client = DeepgramClient(API_KEY, config)

    # ─── API publique ──────────────────────────────────────────────────────────

    def start(self) -> bool:
        if self.is_running:
            return True

        try:
            # Créer la connexion live
            self._connection = self._client.listen.websocket.v("1")

            # ── Enregistrement des événements ─────────────────────────────────
            self._connection.on(
                LiveTranscriptionEvents.Open,
                self._on_open
            )
            self._connection.on(
                LiveTranscriptionEvents.Transcript,
                self._on_transcript
            )
            self._connection.on(
                LiveTranscriptionEvents.Error,
                self._on_error
            )
            self._connection.on(
                LiveTranscriptionEvents.Close,
                self._on_close
            )

            # ── Options de transcription ──────────────────────────────────────
            options = LiveOptions(
                model="nova-2",
                encoding="linear16",
                sample_rate=DEEPGRAM_SAMPLE_RATE,
                channels=1,
                language="multi",
                smart_format=True,
                interim_results=True,
                utterance_end_ms=1000,  # Sans guillemets
                vad_events=True,
            )

            # Démarrer dans un thread séparé
            def _run():
                ok = self._connection.start(options)
                if not ok:
                    print("[Deepgram] ❌ Impossible de démarrer la connexion")

            self._thread = threading.Thread(target=_run, daemon=True)
            self._thread.start()

            # Attendre que la connexion soit ouverte (max 5s)
            deadline = time.time() + 5.0
            while not self.is_running and time.time() < deadline:
                time.sleep(0.05)

            if self.is_running:
                print("[Deepgram] ✅ Connecté via SDK officiel")
            else:
                print("[Deepgram] ⚠ Timeout connexion")

            return self.is_running

        except Exception as e:
            print(f"[Deepgram] ❌ Erreur start : {e}")
            return False

    def stop(self):
        self.is_running = False
        self._cancel_flush_timer()
        try:
            if self._connection:
                self._connection.finish()
        except Exception as e:
            print(f"[Deepgram] Erreur stop : {e}")

    def feed(self, raw_bytes: bytes):
        if not raw_bytes or not self.is_running:
            return

        with self._buffer_lock:
            self._audio_buffer.extend(raw_bytes)
            # On vérifie si on a assez de données
            if len(self._audio_buffer) >= self._MIN_CHUNK_BYTES:
                data = bytes(self._audio_buffer)
                self._audio_buffer.clear()
                # On annule le timer car on envoie manuellement
                if self._flush_timer:
                    self._flush_timer.cancel()
                    self._flush_timer = None
            else:
                # Pas assez de données, on planifie un flush de secours
                self._schedule_flush()
                return

        # Envoi hors du lock pour ne pas bloquer l'AudioEngine
        try:
            if self._connection and self.is_running:
                self._connection.send(data)
        except Exception as e:
            print(f"[Deepgram] Erreur envoi : {e}")

    def set_current_speaker(self, label: str, color: str):
        self._cur_speaker = label
        self._cur_color   = color

    def get_full_transcript(self) -> str:
        return "\n".join(self._full_text)

    def clear_transcript(self):
        self._full_text.clear()
        self._last_lang = "?"

    # ─── Buffer audio ──────────────────────────────────────────────────────────

    def _flush_buffer(self):
        with self._buffer_lock:
            if not self._audio_buffer:
                return
            data = bytes(self._audio_buffer)
            self._audio_buffer.clear()

        try:
            if self._connection and self.is_running:
                self._connection.send(data)
        except Exception as e:
            print(f"[Deepgram] Erreur envoi audio : {e}")

    def _schedule_flush(self):
        self._cancel_flush_timer()
        self._flush_timer = threading.Timer(0.15, self._flush_buffer)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def _cancel_flush_timer(self):
        if self._flush_timer:
            self._flush_timer.cancel()
            self._flush_timer = None

    # ─── Callbacks SDK Deepgram ────────────────────────────────────────────────

    def _on_open(self, *args, **kwargs):
        self.is_running = True
        print("[Deepgram] 🔗 Connexion ouverte")

    def _on_transcript(self, *args, **kwargs):
        """Reçoit les résultats de transcription."""
        try:
            # Le SDK passe le résultat dans kwargs ou args selon la version
            result = kwargs.get("result") or (args[1] if len(args) > 1 else None)
            if result is None:
                return

            # Extraction du transcript
            channel      = result.channel
            alternatives = channel.alternatives
            if not alternatives:
                return

            transcript = alternatives[0].transcript.strip()
            is_final   = result.is_final

            if not transcript:
                return

            # Langue détectée
            detected_lang = getattr(channel, "detected_language", None)
            if detected_lang and detected_lang not in ("?", None, ""):
                if detected_lang != self._last_lang:
                    print(f"[Deepgram] 🌐 Langue : {detected_lang.upper()}")
                self._last_lang = detected_lang

            lang_display = self._last_lang if self._last_lang != "?" else "?"
            ts = datetime.datetime.now().strftime("%H:%M:%S")

            if is_final:
                line = f"[{ts}] {self._cur_speaker}: {transcript}"
                self._full_text.append(line)
                print(f"[Transcript ✓] [{lang_display.upper()}] {line}")

                # → callback vers client.py
                self.on_transcript_segment(
                    transcript,
                    lang_display,
                    ts,
                    self._cur_speaker,
                    self._cur_color,
                )
            else:
                print(f"[Interim ~] [{lang_display.upper()}] {transcript}")

        except Exception as e:
            print(f"[Deepgram] Erreur traitement transcript : {e}")

    def _on_error(self, *args, **kwargs):
        error = kwargs.get("error") or (args[1] if len(args) > 1 else args)
        print(f"[Deepgram] ❌ Erreur : {error}")

    def _on_close(self, *args, **kwargs):
        self.is_running = False
        print("[Deepgram] 🔌 Connexion fermée")