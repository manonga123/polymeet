"""
diarization_engine.py  –  Module F-04 : Identification des Speakers (Diarisation)
──────────────────────────────────────────────────────────────────────────────────
Deux modes automatiquement sélectionnés :

MODE VISIO (réseau) :
  • Les noms viennent directement du réseau WebSocket (client.py)
  • Précision 100% — on sait exactement qui parle
  • Fonctionne dès qu'un participant envoie de l'audio

MODE PRÉSENTIEL (analyse vocale) :
  • pyannote.audio pour détecter les changements de voix
  • Fallback VAD simple si pyannote absent (100% offline)
  • Jusqu'à 10 speakers différenciés
  • Attribution automatique : Speaker 1, Speaker 2…
  • Renommage manuel possible à tout moment

Couleurs des speakers (10 max) :
  #4a9ede #e05050 #4caf50 #e0a030 #9b59b6
  #1abc9c #e67e22 #e91e63 #00bcd4 #8bc34a

Installation optionnelle pour mode présentiel :
  pip install pyannote.audio
  (token HuggingFace gratuit sur https://huggingface.co/pyannote/speaker-diarization)
"""

import threading
import numpy as np
import datetime
import time

# ── Couleurs distinctes pour chaque speaker (10 max) ────────────────────────
SPEAKER_COLORS = [
    "#4a9ede",  # bleu
    "#e05050",  # rouge
    "#4caf50",  # vert
    "#e0a030",  # orange
    "#9b59b6",  # violet
    "#1abc9c",  # turquoise
    "#e67e22",  # orange foncé
    "#e91e63",  # rose
    "#00bcd4",  # cyan
    "#8bc34a",  # vert clair
]

# ── Tentative import pyannote ────────────────────────────────────────────────
try:
    from pyannote.audio import Pipeline
    PYANNOTE_AVAILABLE = True
except ImportError:
    PYANNOTE_AVAILABLE = False

# ─── Paramètres audio ────────────────────────────────────────────────────────
AUDIO_RATE    = 16000
AUDIO_WIDTH   = 2       # paInt16
AUDIO_CHANNELS = 1
VAD_RMS_THRESHOLD  = 400    # seuil RMS pour détecter la parole
VAD_SILENCE_FRAMES = 12     # ~0.8s de silence = changement possible de speaker
VAD_MIN_SPEECH_FRAMES = 6   # frames minimum pour valider une prise de parole


class SpeakerProfile:
    """Représente un speaker identifié."""

    def __init__(self, speaker_id: int, auto_name: str, color: str):
        self.speaker_id   = speaker_id
        self.auto_name    = auto_name       # "Speaker 1", "Speaker 2"…
        self.display_name = auto_name       # modifiable par l'utilisateur
        self.color        = color
        self.first_seen   = datetime.datetime.now().strftime("%H:%M:%S")
        self.last_seen    = self.first_seen
        self.speech_count = 0               # nombre de prises de parole

    def rename(self, new_name: str):
        if new_name.strip():
            self.display_name = new_name.strip()

    @property
    def label(self) -> str:
        return self.display_name

    def to_dict(self) -> dict:
        return {
            "id":           self.speaker_id,
            "auto_name":    self.auto_name,
            "display_name": self.display_name,
            "color":        self.color,
            "first_seen":   self.first_seen,
            "last_seen":    self.last_seen,
            "speech_count": self.speech_count,
        }


class DiarizationEngine:
    """
    Moteur de diarisation — deux modes :

    1. Mode VISIO  : feed_network_speaker(name) — appelé depuis client.py
                     quand on reçoit un chunk audio réseau d'un participant.

    2. Mode PRÉSENTIEL : feed_local_audio(raw_bytes) — analyse les voix
                         via pyannote.audio ou VAD simple.

    Callback : on_speaker_change(speaker: SpeakerProfile, timestamp: str)
    """

    MODE_NETWORK     = "network"
    MODE_PRESENTIEL  = "presentiel"

    def __init__(self, on_speaker_change=None, on_speakers_updated=None):
        """
        on_speaker_change(speaker, timestamp) :
            Appelé quand un nouveau speaker est détecté.
        on_speakers_updated(speakers_dict) :
            Appelé quand la liste des speakers change (ajout/renommage).
            speakers_dict = {name: SpeakerProfile}
        """
        self.on_speaker_change   = on_speaker_change
        self.on_speakers_updated = on_speakers_updated

        self._mode              = self.MODE_NETWORK
        self._speakers          = {}        # display_name → SpeakerProfile
        self._network_speakers  = {}        # name réseau  → SpeakerProfile
        self._current_speaker   = None      # SpeakerProfile actif
        self._lock              = threading.Lock()

        # État VAD pour mode présentiel
        self._vad_silence_count = 0
        self._vad_speech_count  = 0
        self._vad_speaking      = False
        self._vad_buffer        = bytearray()

        # pyannote pipeline (optionnel)
        self._pyannote           = None
        self._next_speaker_idx   = 1        # compteur auto "Speaker N"

    # ── Démarrage ────────────────────────────────────────────────────────────

    def start(self, mode: str = MODE_NETWORK, hf_token: str = ""):
        """
        mode      : MODE_NETWORK ou MODE_PRESENTIEL
        hf_token  : token HuggingFace pour pyannote (mode présentiel seulement)
        """
        self._mode = mode

        if mode == self.MODE_PRESENTIEL and PYANNOTE_AVAILABLE and hf_token:
            try:
                print("[Diarization] ⏳ Chargement pyannote.audio…")
                self._pyannote = Pipeline.from_pretrained(
                    "pyannote/speaker-diarization-3.1",
                    use_auth_token=hf_token)
                print("[Diarization] ✅ pyannote chargé.")
            except Exception as e:
                print(f"[Diarization] ⚠ pyannote échoué, fallback VAD : {e}")
                self._pyannote = None
        elif mode == self.MODE_PRESENTIEL:
            print("[Diarization] ℹ  Mode présentiel — VAD simple (sans pyannote).")

        print(f"[Diarization] ▶ Démarré en mode '{mode}'.")
        return True

    def stop(self):
        print("[Diarization] Arrêté.")

    # ── Mode VISIO — alimentation par noms réseau ─────────────────────────────

    def feed_network_speaker(self, network_name: str):
        """
        Appelé depuis client.py quand on reçoit un chunk audio d'un participant.
        network_name = nom du participant tel qu'il s'est connecté.
        """
        with self._lock:
            if network_name not in self._network_speakers:
                profile = self._create_speaker(
                    display_name=network_name,
                    auto_name=network_name)
                self._network_speakers[network_name] = profile
                self._notify_updated()

            profile = self._network_speakers[network_name]
            profile.speech_count += 1
            profile.last_seen = datetime.datetime.now().strftime("%H:%M:%S")

            if self._current_speaker != profile:
                self._current_speaker = profile
                ts = datetime.datetime.now().strftime("%H:%M:%S")
                if self.on_speaker_change:
                    self.on_speaker_change(profile, ts)

    # ── Mode PRÉSENTIEL — alimentation par audio local ────────────────────────

    def feed_local_audio(self, raw_bytes: bytes):
        """
        Appelé pour chaque chunk audio micro en mode présentiel.
        Utilise pyannote si disponible, sinon VAD simple.
        """
        if self._mode != self.MODE_PRESENTIEL:
            return

        if self._pyannote:
            self._feed_pyannote(raw_bytes)
        else:
            self._feed_vad(raw_bytes)

    def _feed_vad(self, raw_bytes: bytes):
        """
        Détection de changement de speaker par analyse de pauses (VAD).
        Logique : une pause > ~0.8s → potentiel changement de speaker.
        """
        samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
        rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0

        with self._lock:
            if rms > VAD_RMS_THRESHOLD:
                # Parole détectée
                self._vad_silence_count = 0
                self._vad_speech_count += 1

                if not self._vad_speaking and self._vad_speech_count >= VAD_MIN_SPEECH_FRAMES:
                    # Nouvelle prise de parole après silence
                    self._vad_speaking = True
                    self._on_new_speech_segment()

            else:
                # Silence
                if self._vad_speaking:
                    self._vad_silence_count += 1
                    if self._vad_silence_count >= VAD_SILENCE_FRAMES:
                        # Fin de la prise de parole
                        self._vad_speaking      = False
                        self._vad_speech_count  = 0
                        self._vad_silence_count = 0

    def _on_new_speech_segment(self):
        """
        Appelé quand une nouvelle prise de parole est détectée en mode VAD.
        Attribue le speaker courant ou en crée un nouveau si c'est le premier.
        Note : sans pyannote, on ne peut pas différencier les voix, donc on
        garde le speaker courant à moins que ce soit le tout premier.
        """
        if not self._current_speaker:
            # Premier speaker de la session
            profile = self._get_or_create_presentiel_speaker(1)
            self._current_speaker = profile
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            if self.on_speaker_change:
                self.on_speaker_change(profile, ts)
        else:
            # Incrémenter le compteur du speaker courant
            self._current_speaker.speech_count += 1
            self._current_speaker.last_seen = datetime.datetime.now().strftime("%H:%M:%S")

    def _feed_pyannote(self, raw_bytes: bytes):
        """Accumule l'audio et envoie à pyannote par blocs de 5s."""
        self._vad_buffer.extend(raw_bytes)
        buffer_secs = len(self._vad_buffer) / (AUDIO_RATE * AUDIO_WIDTH)

        if buffer_secs >= 5.0:
            audio_data = bytes(self._vad_buffer)
            self._vad_buffer = bytearray()
            threading.Thread(
                target=self._run_pyannote,
                args=(audio_data,),
                daemon=True).start()

    def _run_pyannote(self, audio_data: bytes):
        """Exécute pyannote sur un bloc audio et met à jour les speakers."""
        try:
            audio_np = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
            audio_np /= 32768.0

            import torch
            audio_tensor = torch.tensor(audio_np).unsqueeze(0)
            diarization = self._pyannote({
                "waveform": audio_tensor,
                "sample_rate": AUDIO_RATE
            })

            for turn, _, speaker_label in diarization.itertracks(yield_label=True):
                # speaker_label = "SPEAKER_00", "SPEAKER_01"…
                idx = int(speaker_label.split("_")[-1]) + 1
                profile = self._get_or_create_presentiel_speaker(idx)

                with self._lock:
                    if self._current_speaker != profile:
                        self._current_speaker = profile
                        profile.speech_count += 1
                        profile.last_seen = datetime.datetime.now().strftime("%H:%M:%S")
                        ts = datetime.datetime.now().strftime("%H:%M:%S")
                        if self.on_speaker_change:
                            self.on_speaker_change(profile, ts)

        except Exception as e:
            print(f"[Diarization] ❌ Erreur pyannote : {e}")

    # ── Gestion des speakers ──────────────────────────────────────────────────

    def _get_or_create_presentiel_speaker(self, idx: int) -> SpeakerProfile:
        """Retourne le profil d'un speaker présentiel par index, le crée si absent."""
        auto_name = f"Speaker {idx}"
        with self._lock:
            if auto_name not in self._speakers:
                profile = self._create_speaker(
                    display_name=auto_name,
                    auto_name=auto_name)
                self._notify_updated()
            return self._speakers[auto_name]

    def _create_speaker(self, display_name: str, auto_name: str) -> SpeakerProfile:
        """Crée un nouveau SpeakerProfile et l'ajoute au dictionnaire."""
        idx    = len(self._speakers)
        color  = SPEAKER_COLORS[idx % len(SPEAKER_COLORS)]
        profile = SpeakerProfile(
            speaker_id=idx + 1,
            auto_name=auto_name,
            color=color)
        if display_name != auto_name:
            profile.display_name = display_name
        self._speakers[auto_name] = profile
        return profile

    def _notify_updated(self):
        """Notifie l'UI que la liste des speakers a changé."""
        if self.on_speakers_updated:
            speakers_copy = dict(self._speakers)
            # Fusionner avec les speakers réseau
            for k, v in self._network_speakers.items():
                if v.auto_name not in speakers_copy:
                    speakers_copy[v.auto_name] = v
            self.on_speakers_updated(speakers_copy)

    def rename_speaker(self, auto_name: str, new_name: str):
        """Renomme un speaker (auto_name = clé, new_name = nouveau label)."""
        with self._lock:
            # Chercher dans speakers et network_speakers
            target = (self._speakers.get(auto_name)
                      or self._network_speakers.get(auto_name))
            if target:
                target.rename(new_name)
                self._notify_updated()
                print(f"[Diarization] Renommé : '{auto_name}' → '{new_name}'")

    def add_network_participant(self, name: str):
        """
        Pré-enregistre un participant réseau dès qu'il rejoint
        (même avant qu'il parle).
        """
        with self._lock:
            if name not in self._network_speakers:
                profile = self._create_speaker(
                    display_name=name,
                    auto_name=name)
                self._network_speakers[name] = profile
                self._notify_updated()

    def remove_network_participant(self, name: str):
        """Retire un participant réseau qui a quitté."""
        with self._lock:
            if name in self._network_speakers:
                auto = self._network_speakers[name].auto_name
                self._network_speakers.pop(name, None)
                self._speakers.pop(auto, None)
                self._notify_updated()

    # ── Accesseurs ───────────────────────────────────────────────────────────

    @property
    def current_speaker(self):
        return self._current_speaker

    def get_all_speakers(self) -> dict:
        """Retourne tous les speakers (réseau + présentiel)."""
        with self._lock:
            merged = dict(self._speakers)
            for k, v in self._network_speakers.items():
                if v.auto_name not in merged:
                    merged[v.auto_name] = v
            return merged

    def get_speaker_count(self) -> int:
        return len(self.get_all_speakers())

    def export_speakers_log(self) -> str:
        """Génère un résumé texte des speakers pour le rapport."""
        lines = ["Participants identifiés :", "─" * 30]
        for profile in self.get_all_speakers().values():
            lines.append(
                f"  • {profile.label:<20} "
                f"couleur:{profile.color}  "
                f"prises de parole:{profile.speech_count}  "
                f"vu à:{profile.first_seen}")
        return "\n".join(lines)