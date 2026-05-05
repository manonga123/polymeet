import sys
import os
import threading
import pyaudio

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
API_KEY = "87f8ff324ecf474faa7d5141d2fa608d"   # <-- remplacez ici
SAMPLE_RATE = 16_000
CHUNK_SIZE  = 3200   # 200ms à 16kHz


# ─────────────────────────────────────────
# Générateur microphone
# ─────────────────────────────────────────
def micro_stream(stop_event: threading.Event):
    """Capte le micro et génère des chunks bytes."""
    import os

    # Supprimer les messages ALSA/JACK parasites
    devnull = open(os.devnull, 'w')
    old_stderr = os.dup(2)
    os.dup2(devnull.fileno(), 2)

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    os.dup2(old_stderr, 2)
    os.close(old_stderr)
    devnull.close()

    print("🎙️  Enregistrement en cours... (Ctrl+C pour arrêter)\n")
    print("─" * 55)

    try:
        while not stop_event.is_set():
            yield stream.read(CHUNK_SIZE, exception_on_overflow=False)
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


# ─────────────────────────────────────────
# Transcription avec le nouveau StreamingClient
# ─────────────────────────────────────────
def lancer_transcription():
    from assemblyai.streaming.v3.client import StreamingClient
    from assemblyai.streaming.v3.models import (
        StreamingClientOptions,
        StreamingParameters,
        StreamingEvents,
        TurnEvent,
        BeginEvent,
        ErrorEvent,
    )

    options = StreamingClientOptions(
        api_key=API_KEY,
        api_host="streaming.assemblyai.com",
    )

    params = StreamingParameters(
        sample_rate=SAMPLE_RATE,
        encoding="pcm_s16le",
        speech_model="universal-streaming-multilingual",
    )

    client = StreamingClient(options)
    stop_event = threading.Event()

    # ─── EVENTS ───────────────────────────

    def on_begin(client, evt: BeginEvent):
        print("✅ Connecté au streaming\n")

    def on_turn(evt: TurnEvent):
        if not evt.transcript:
            return

        if evt.end_of_turn:
            print(f"\n📝 FINAL : {evt.transcript}")
        else:
            print(f"💬 {evt.transcript}", end="\r")

    def on_error(evt: ErrorEvent):
        print(f"\n❌ Erreur : {evt.error}")
        stop_event.set()

    # ─── BIND EVENTS ─────────────────────

    client.on(StreamingEvents.Begin, on_begin)
    client.on(StreamingEvents.Turn, on_turn)
    client.on(StreamingEvents.Error, on_error)

    # ─── START ───────────────────────────

    try:
        client.connect(params)
        client.stream(micro_stream(stop_event))
    except KeyboardInterrupt:
        print("\n⏹️ Arrêt")
    finally:
        stop_event.set()
        client.disconnect()

# ─────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 55)
    print("   TRANSCRIPTION TEMPS RÉEL — AssemblyAI 0.60+")
    print("=" * 55)

    if API_KEY == "VOTRE_CLE_API_ASSEMBLYAI":
        print("\n⚠️  Clé API non configurée !")
        print("   Éditez la variable API_KEY en haut du fichier.")
        sys.exit(1)

    lancer_transcription()