import sys
import os
import asyncio
import json
import pyaudio
import websockets

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
API_KEY = "f7ec7afccd134538b06b3626b2d87c4a00144f6b"
SAMPLE_RATE = 16_000
CHUNK_SIZE = 3200  # 200ms de données

# URL avec les paramètres de transcription (Nova-2, Français, etc.)
URL = (
    f"wss://api.deepgram.com/v1/listen?"
    f"model=nova-2&language=fr&smart_format=true&"
    f"interim_results=true&encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1"
)


# ─────────────────────────────────────────
# GESTION AUDIO (PyAudio)
# ─────────────────────────────────────────
def ouvrir_pyaudio():
    devnull = open(os.devnull, 'w')
    old_stderr = os.dup(2)
    os.dup2(devnull.fileno(), 2)
    pa = pyaudio.PyAudio()
    os.dup2(old_stderr, 2)
    os.close(old_stderr)
    devnull.close()
    return pa


def verifier_micro() -> bool:
    pa = ouvrir_pyaudio()
    count = pa.get_device_count()
    micros = [pa.get_device_info_by_index(i)["name"] for i in range(count)
              if pa.get_device_info_by_index(i)["maxInputChannels"] > 0]
    pa.terminate()

    if not micros:
        print("❌ Aucun microphone détecté !")
        return False

    print(f"🎤 Microphones disponibles ({len(micros)}) :")
    for m in micros:
        print(f"   -> {m}")
    return True


# ─────────────────────────────────────────
# LOGIQUE ASYNCHRONE DE TRANSCRIPTION
# ─────────────────────────────────────────
async def lancer_transcription():
    # Headers d'authentification
    headers = {"Authorization": f"Token {API_KEY}"}

    try:
        async with websockets.connect(URL, additional_headers=headers) as ws:
            print("\n✅ Connecté à Deepgram via WebSocket.")
            print("🎤 Parlez... (Ctrl+C pour arrêter)\n" + "-" * 50)

            pa = ouvrir_pyaudio()
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE
            )

            # Fonction pour envoyer l'audio
            async def envoyer_audio():
                try:
                    while True:
                        # Lire les données du micro
                        data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                        # Envoyer les bytes bruts via WebSocket
                        await ws.send(data)
                        await asyncio.sleep(0.01)  # Petit repos pour la boucle event loop
                except Exception as e:
                    print(f"\n[Erreur Envoi] {e}")

            # Fonction pour recevoir les résultats
            async def recevoir_resultats():
                try:
                    async for message in ws:
                        resp = json.loads(message)

                        # Vérifier s'il y a une transcription
                        if "channel" in resp:
                            transcript = resp["channel"]["alternatives"][0]["transcript"]
                            is_final = resp.get("is_final", False)

                            if transcript:
                                if is_final:
                                    print(f"\rFINAL    : {transcript}")
                                    print("En cours : ", end="", flush=True)
                                else:
                                    # Affichage dynamique sur la même ligne
                                    print(f"\rEn cours : {transcript:<60}", end="", flush=True)
                except Exception as e:
                    print(f"\n[Erreur Réception] {e}")

            # Lancer les deux tâches en parallèle
            await asyncio.gather(envoyer_audio(), recevoir_resultats())

    except Exception as e:
        print(f"❌ Impossible de se connecter : {e}")
    finally:
        if 'stream' in locals():
            stream.stop_stream()
            stream.close()
        if 'pa' in locals():
            pa.terminate()


# ─────────────────────────────────────────
# POINT D'ENTRÉE
# ─────────────────────────────────────────
if __name__ == "__main__":
    if API_KEY == "VOTRE_CLE_API_DEEPGRAM":
        print("\n⚠️ Clé API non configurée dans le script !")
        sys.exit(1)

    if verifier_micro():
        try:
            asyncio.run(lancer_transcription())
        except KeyboardInterrupt:
            print("\n\n👋 Session terminée par l'utilisateur.")