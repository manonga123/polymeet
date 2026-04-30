"""
client.py  –  Client de salle vidéo + audio partagée
─────────────────────────────────────────────────────
  [F-02] Contrôle de l'Enregistrement :
  • Boutons Start / Pause / Reprendre / Arrêter
  • Raccourcis clavier (Ctrl+R=Start, Ctrl+P=Pause, Ctrl+S=Stop)
  • Indicateur de durée de réunion en temps réel
  • Sauvegarde automatique toutes les 5 minutes (anti-crash)
  • Horodatage précis de chaque intervention dans un fichier log
  • Export MP3 via pydub (dossier recordings/)
  ─────────────────────────────────────────────────────
  [F-03] Transcription Automatique :
  • Whisper AI local (modèle "small") — précision > 90%
  • Latence ~1-2 secondes
  • Détection automatique de la langue
  • Onglet "📝 Transcription" dans le panneau de droite
  • Texte éditable en direct (correction manuelle)
  • Export TXT de la transcription complète
  ─────────────────────────────────────────────────────
  [F-05/F-06] Traduction Multilingue :
  • 9 langues : FR, EN, AR, ES, DE, ZH, PT, IT, MG
  • Traduction tous azimuts (72 paires)
  • Détection automatique de la langue source via Whisper
  • Fallback pivot anglais/français si paire directe absente
"""
import customtkinter as ctk
import tkinter as tk
import cv2
import threading
import asyncio
import websockets
import json
import base64
import math
import numpy as np
from PIL import Image, ImageTk
import pyaudio
import tkinter.messagebox as msgbox
import warnings
import os
import wave
import time
import datetime
from collections import deque

# ── pydub pour export MP3 ────────────────────────────────────────────────────
try:
    from pydub import AudioSegment

    # Fix Windows : ffmpeg pas toujours dans le PATH selon le terminal utilisé
    import shutil, sys
    _ffmpeg = shutil.which("ffmpeg")
    if not _ffmpeg:
        # Chercher dans le venv actif en priorité
        _venv_dirs = []
        if hasattr(sys, "prefix"):
            _venv_dirs += [
                os.path.join(sys.prefix, "bin", "ffmpeg.exe"),
                os.path.join(sys.prefix, "Scripts", "ffmpeg.exe"),
                os.path.join(sys.prefix, "Library", "bin", "ffmpeg.exe"),
            ]
        # Emplacements courants sur Windows
        _candidates = _venv_dirs + [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe",
            os.path.expanduser(r"~\ffmpeg\bin\ffmpeg.exe"),
            os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
        ]
        for c in _candidates:
            c = os.path.expandvars(c)
            if os.path.isfile(c):
                _ffmpeg = c
                break

    if _ffmpeg:
        AudioSegment.converter = _ffmpeg
        AudioSegment.ffmpeg    = _ffmpeg
        print(f"[Audio] ffmpeg trouvé : {_ffmpeg}")
    else:
        # Chemin WinGet connu — à adapter si différent sur votre machine
        _winget_path = os.path.expanduser(
            r"~\AppData\Local\Microsoft\WinGet\Packages"
            r"\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe"
            r"\ffmpeg-8.1-full_build\bin\ffmpeg.exe")
        if os.path.isfile(_winget_path):
            AudioSegment.converter = _winget_path
            AudioSegment.ffmpeg    = _winget_path
            print(f"[Audio] ffmpeg WinGet trouvé : {_winget_path}")
        else:
            print("[Audio] ⚠ ffmpeg introuvable — export en .wav (MP3 désactivé)")
            print("         Lancez depuis le terminal VSCode ou ajoutez ffmpeg au PATH système")

    PYDUB_AVAILABLE = True
except ImportError:
    PYDUB_AVAILABLE = False
    print("[Record] pydub non disponible – sauvegarde en .wav uniquement")

warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

# ── TranscriptionEngine [F-03] ───────────────────────────────────────────────
try:
    from transcription_engine import TranscriptionEngine
    TRANSCRIPTION_AVAILABLE = True
except ImportError:
    TRANSCRIPTION_AVAILABLE = False
    print("[Client] transcription_engine.py introuvable — F-03 désactivé")

# ── DiarizationEngine [F-04] ─────────────────────────────────────────────────
try:
    from diarization_engine import DiarizationEngine, SPEAKER_COLORS
    DIARIZATION_AVAILABLE = True
except ImportError:
    DIARIZATION_AVAILABLE = False
    print("[Client] diarization_engine.py introuvable — F-04 désactivé")

# ── TranslationEngine [F-05/F-06] ────────────────────────────────────────────
try:
    from translation_engine import TranslationEngine, SUPPORTED_LANGUAGES, LANG_NAMES
    TRANSLATION_AVAILABLE = True
except ImportError:
    TRANSLATION_AVAILABLE = False
    print("[Client] translation_engine.py introuvable — F-05 désactivé")

# ── AnalysisEngine [F-07/F-08] ───────────────────────────────────────────────
try:
    from analysis_engine import AnalysisEngine, MeetingAnalysis
    ANALYSIS_AVAILABLE = True
except ImportError:
    ANALYSIS_AVAILABLE = False
    print("[Client] analysis_engine.py introuvable — F-07 désactivé")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ─── Paramètres réseau ──────────────────────────────────────────────────────
DROIDCAM_IP   = "192.168.43.102"
DROIDCAM_PORT = 4747
SERVER_IP     = "192.168.43.63"
SERVER_PORT   = 9765

# ─── Paramètres vidéo ───────────────────────────────────────────────────────
FRAME_QUALITY     = 50
FRAME_RESIZE      = (320, 240)
FRAME_INTERVAL_MS = 50        # ~20 fps

# ─── Paramètres audio ───────────────────────────────────────────────────────
# 44100 Hz = taux universel compatible avec tous les micros (DroidCam inclus)
AUDIO_RATE      = 44100
AUDIO_CHANNELS  = 1
AUDIO_FORMAT    = pyaudio.paInt16
AUDIO_CHUNK     = 2048         # plus grand chunk pour 44100 Hz
AUDIO_VAD_RMS   = 300
AUDIO_QUEUE_MAX = 8
# Taux cible pour Whisper (il faut 16000 Hz) — on rééchantillonne à la volée
WHISPER_RATE    = 16000

# ─── Paramètres enregistrement [F-02] ───────────────────────────────────────
RECORDINGS_DIR      = "recordings"
AUTOSAVE_INTERVAL_S = 300          # 5 minutes
os.makedirs(RECORDINGS_DIR, exist_ok=True)


# ─── Liste des microphones disponibles ──────────────────────────────────────
def list_microphones():
    """
    Retourne une liste de tuples (device_index, label, sample_rate)
    pour tous les micros disponibles sur le système.
    """
    mics = []
    try:
        pa = pyaudio.PyAudio()
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info['maxInputChannels'] > 0:
                rate = int(info['defaultSampleRate'])
                label = f"🎤 [{i}] {info['name']} ({rate} Hz)"
                mics.append((i, label, rate))
        pa.terminate()
    except Exception as e:
        print(f"[Mic] Erreur détection : {e}")
    return mics


# ─── Détection de TOUTES les sources caméra disponibles ─────────────────────
def list_all_camera_sources():
    sources = []
    url = f"http://{DROIDCAM_IP}:{DROIDCAM_PORT}/mjpegfeed"
    cap = cv2.VideoCapture(url)
    if cap.isOpened():
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            sources.append((url, f"📱 DroidCam ({DROIDCAM_IP})"))
    for idx in range(5):
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            if ret and frame is not None:
                sources.append((idx, f"💻 Webcam {idx}"))
    return sources


# ─── Tuile vidéo ─────────────────────────────────────────────────────────────
class VideoTile(tk.Frame):
    def __init__(self, parent, name, is_me=False, **kwargs):
        bg = "#0d1117"
        super().__init__(parent, bg=bg, **kwargs)
        self.name  = name
        self.is_me = is_me

        bar_bg = "#1a4a8a" if is_me else "#2a2a4a"
        tag    = " (Vous)" if is_me else ""

        self.name_bar = tk.Frame(self, bg=bar_bg, height=22)
        self.name_bar.pack(side="bottom", fill="x")
        self.name_bar.pack_propagate(False)

        self.name_lbl = tk.Label(
            self.name_bar, text=f"  {name}{tag}",
            bg=bar_bg, fg="white",
            font=("Arial", 10, "bold"), anchor="w")
        self.name_lbl.pack(side="left", fill="both", expand=True)

        self.mic_indicator = tk.Label(
            self.name_bar, text="🎤", bg=bar_bg,
            font=("Arial", 9), fg="#555")
        self.mic_indicator.pack(side="right", padx=4)

        self.img_label = tk.Label(
            self, text="⏳\nEn attente…",
            bg=bg, fg="#555", font=("Arial", 16))
        self.img_label.pack(expand=True, fill="both")

    def update_frame(self, pil_image):
        if not self.winfo_exists():
            return
        try:
            w = max(self.winfo_width()  - 2, 120)
            h = max(self.winfo_height() - 26, 80)
            img   = pil_image.resize((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.img_label.configure(image=photo, text="")
            self.img_label.image = photo
        except Exception as e:
            print(f"[Tile] Erreur affichage : {e}")

    def set_speaking(self, speaking: bool):
        if not self.winfo_exists():
            return
        color = "#4caf50" if speaking else "#555"
        self.mic_indicator.configure(fg=color)


# ─── Moteur audio ────────────────────────────────────────────────────────────
class AudioEngine:
    def __init__(self, on_chunk_ready, on_record_chunk=None,
                 device_index=None, device_rate=None):
        self.on_chunk_ready  = on_chunk_ready
        self.on_record_chunk = on_record_chunk
        self.device_index    = device_index       # None = périphérique par défaut
        self.device_rate     = device_rate or AUDIO_RATE
        self.pa              = None
        self.in_stream       = None
        self.out_stream      = None
        self.muted           = False
        self.running         = False
        self.audio_queues    = {}
        self._lock           = threading.Lock()
        self._pb_thread      = None

    def start(self):
        try:
            self.pa = pyaudio.PyAudio()
        except Exception as e:
            print(f"[Audio] PyAudio indisponible : {e}")
            return False

        # Résoudre le périphérique par défaut si non spécifié
        if self.device_index is None:
            try:
                info = self.pa.get_default_input_device_info()
                self.device_index = info["index"]
                self.device_rate  = int(info["defaultSampleRate"])
            except Exception:
                self.device_index = None
                self.device_rate  = AUDIO_RATE

        print(f"[Audio] Micro index={self.device_index}, taux={self.device_rate} Hz")

        try:
            kwargs = dict(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=self.device_rate,
                input=True,
                frames_per_buffer=AUDIO_CHUNK,
                stream_callback=self._capture_callback
            )
            if self.device_index is not None:
                kwargs["input_device_index"] = self.device_index
            self.in_stream = self.pa.open(**kwargs)
            self.in_stream.start_stream()
            print(f"[Audio] ✅ Capture micro démarrée ({self.device_rate} Hz)")
        except Exception as e:
            print(f"[Audio] ❌ Micro : {e}")
            return False

        try:
            self.out_stream = self.pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=self.device_rate,
                output=True,
                frames_per_buffer=AUDIO_CHUNK
            )
            print(f"[Audio] ✅ Lecture audio démarrée")
        except Exception as e:
            print(f"[Audio] ❌ HP : {e}")
            return False

        self.running = True
        self._pb_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._pb_thread.start()
        return True

    def stop(self):
        self.running = False
        for stream in (self.in_stream, self.out_stream):
            if stream:
                try:
                    stream.stop_stream(); stream.close()
                except Exception:
                    pass
        self.in_stream = self.out_stream = None
        if self.pa:
            try: self.pa.terminate()
            except Exception: pass
            self.pa = None
        with self._lock:
            self.audio_queues.clear()

    def toggle_mute(self):
        self.muted = not self.muted
        return self.muted

    def receive_chunk(self, name: str, b64_chunk: str):
        try:
            raw = base64.b64decode(b64_chunk)
        except Exception:
            return
        with self._lock:
            if name not in self.audio_queues:
                self.audio_queues[name] = deque(maxlen=AUDIO_QUEUE_MAX)
            self.audio_queues[name].append(raw)

    def remove_participant(self, name: str):
        with self._lock:
            self.audio_queues.pop(name, None)

    def resample_to_whisper(self, raw_bytes: bytes) -> bytes:
        """Rééchantillonne de device_rate vers WHISPER_RATE (16000 Hz)."""
        if self.device_rate == WHISPER_RATE:
            return raw_bytes
        try:
            samples  = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            ratio    = WHISPER_RATE / self.device_rate
            new_len  = max(1, int(len(samples) * ratio))
            resampled = np.interp(
                np.linspace(0, len(samples) - 1, new_len),
                np.arange(len(samples)), samples
            ).astype(np.int16)
            return resampled.tobytes()
        except Exception:
            return raw_bytes

    def _capture_callback(self, in_data, frame_count, time_info, status):
        if in_data:
            if self.on_record_chunk:
                self.on_record_chunk(in_data)
            if not self.muted:
                samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0
                if rms > AUDIO_VAD_RMS:
                    b64 = base64.b64encode(in_data).decode()
                    self.on_chunk_ready(b64)
        return (None, pyaudio.paContinue)

    def _playback_loop(self):
        silence = b'\x00' * (AUDIO_CHUNK * 2)
        while self.running:
            mixed = None
            with self._lock:
                names = list(self.audio_queues.keys())
            for name in names:
                with self._lock:
                    q = self.audio_queues.get(name)
                    chunk = q.popleft() if q and len(q) > 0 else None
                if chunk and len(chunk) == AUDIO_CHUNK * 2:
                    arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    mixed = arr if mixed is None else mixed + arr
            if mixed is not None:
                mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
                data = mixed.tobytes()
            else:
                data = silence
            if self.out_stream and self.running:
                try:
                    self.out_stream.write(data)
                except Exception:
                    pass


# ═══════════════════════════════════════════════════════════════════════════════
# [F-02] RecordingEngine — Contrôle de l'Enregistrement
# ═══════════════════════════════════════════════════════════════════════════════
class RecordingEngine:
    """
    Gère l'enregistrement audio local avec :
    - États : IDLE / RECORDING / PAUSED
    - Sauvegarde automatique toutes les 5 minutes (anti-crash)
    - Horodatage de chaque intervention (via callback externe)
    - Export final en MP3 (via pydub) ou WAV fallback
    """

    STATE_IDLE      = "idle"
    STATE_RECORDING = "recording"
    STATE_PAUSED    = "paused"

    def __init__(self):
        self.state          = self.STATE_IDLE
        self._frames        = []          # buffer audio courant
        self._all_frames    = []          # buffer complet (pour export final)
        self._lock          = threading.Lock()
        self._start_time    = None        # datetime de début de réunion
        self._pause_time    = None        # datetime de la dernière pause
        self._total_paused  = 0.0         # secondes cumulées en pause
        self._session_name  = ""          # nom du fichier de session
        self._log_entries   = []          # horodatages [(time_str, speaker, note)]
        self._autosave_timer = None

    # ── État ────────────────────────────────────────────────────────────────
    @property
    def is_recording(self):
        return self.state == self.STATE_RECORDING

    @property
    def elapsed_seconds(self):
        """Durée réelle (pause déduite)."""
        if self._start_time is None:
            return 0.0
        total = (datetime.datetime.now() - self._start_time).total_seconds()
        paused = self._total_paused
        if self.state == self.STATE_PAUSED and self._pause_time:
            paused += (datetime.datetime.now() - self._pause_time).total_seconds()
        return max(0.0, total - paused)

    # ── Contrôles ───────────────────────────────────────────────────────────
    def start(self, session_name: str = ""):
        if self.state != self.STATE_IDLE:
            return False
        self._start_time   = datetime.datetime.now()
        self._total_paused = 0.0
        self._pause_time   = None
        self._frames       = []
        self._all_frames   = []
        self._log_entries  = []
        self._session_name = session_name or self._start_time.strftime("reunion_%Y%m%d_%H%M%S")
        self.state = self.STATE_RECORDING
        self._schedule_autosave()
        self._log("SYSTEM", "Enregistrement démarré")
        print(f"[Record] ▶ Démarré : {self._session_name}")
        return True

    def pause(self):
        if self.state != self.STATE_RECORDING:
            return False
        self._pause_time = datetime.datetime.now()
        self.state = self.STATE_PAUSED
        self._cancel_autosave()
        self._log("SYSTEM", "Enregistrement mis en pause")
        print("[Record] ⏸ Pausé")
        return True

    def resume(self):
        if self.state != self.STATE_PAUSED:
            return False
        if self._pause_time:
            self._total_paused += (datetime.datetime.now() - self._pause_time).total_seconds()
        self._pause_time = None
        self.state = self.STATE_RECORDING
        self._schedule_autosave()
        self._log("SYSTEM", "Enregistrement repris")
        print("[Record] ▶ Repris")
        return True

    def stop(self):
        if self.state == self.STATE_IDLE:
            return None
        self._cancel_autosave()
        if self.state == self.STATE_PAUSED and self._pause_time:
            self._total_paused += (datetime.datetime.now() - self._pause_time).total_seconds()
        self._log("SYSTEM", f"Enregistrement arrêté — durée : {self._fmt_duration(self.elapsed_seconds)}")
        self.state = self.STATE_IDLE

        # Sauvegarde finale
        path = self._save(self._all_frames, self._session_name, final=True)
        self._save_log()
        print(f"[Record] ⏹ Arrêté — fichier : {path}")
        return path

    # ── Réception des chunks audio ──────────────────────────────────────────
    def add_chunk(self, raw_bytes: bytes):
        if self.state != self.STATE_RECORDING:
            return
        with self._lock:
            self._frames.append(raw_bytes)
            self._all_frames.append(raw_bytes)

    # ── Horodatage d'une intervention ───────────────────────────────────────
    def log_intervention(self, speaker: str, note: str = ""):
        if self.state == self.STATE_IDLE:
            return
        self._log(speaker, note or "Intervention")

    def _log(self, speaker: str, note: str):
        ts  = datetime.datetime.now().strftime("%H:%M:%S")
        dur = self._fmt_duration(self.elapsed_seconds)
        entry = f"[{ts}] (+{dur})  {speaker}: {note}"
        self._log_entries.append(entry)

    # ── Sauvegarde automatique ───────────────────────────────────────────────
    def _schedule_autosave(self):
        self._autosave_timer = threading.Timer(
            AUTOSAVE_INTERVAL_S, self._autosave_callback)
        self._autosave_timer.daemon = True
        self._autosave_timer.start()

    def _cancel_autosave(self):
        if self._autosave_timer:
            self._autosave_timer.cancel()
            self._autosave_timer = None

    def _autosave_callback(self):
        if self.state != self.STATE_RECORDING:
            return
        with self._lock:
            frames_copy = list(self._frames)
            self._frames = []
        ts   = datetime.datetime.now().strftime("%H%M%S")
        name = f"{self._session_name}_autosave_{ts}"
        path = self._save(frames_copy, name, final=False)
        print(f"[Record] 💾 Autosave → {path}")
        self._schedule_autosave()   # replanifier

    # ── Export WAV / MP3 ────────────────────────────────────────────────────
    def _save(self, frames: list, name: str, final: bool) -> str:
        if not frames:
            return ""

        wav_path = os.path.join(RECORDINGS_DIR, f"{name}.wav")
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(AUDIO_CHANNELS)
                wf.setsampwidth(2)          # paInt16 = 2 bytes
                wf.setframerate(AUDIO_RATE)
                wf.writeframes(b"".join(frames))
        except Exception as e:
            print(f"[Record] ❌ Erreur WAV : {e}")
            return ""

        # Conversion MP3 si pydub disponible (final uniquement)
        if final and PYDUB_AVAILABLE:
            mp3_path = os.path.join(RECORDINGS_DIR, f"{name}.mp3")
            try:
                audio = AudioSegment.from_wav(wav_path)
                audio.export(mp3_path, format="mp3", bitrate="64k")
                os.remove(wav_path)
                return mp3_path
            except Exception as e:
                print(f"[Record] ⚠ MP3 échoué, conservé en WAV : {e}")
                return wav_path

        return wav_path

    def _save_log(self):
        if not self._log_entries:
            return
        log_path = os.path.join(RECORDINGS_DIR, f"{self._session_name}_log.txt")
        try:
            with open(log_path, "w", encoding="utf-8") as f:
                f.write(f"PolyMeet — Journal d'horodatage\n")
                f.write(f"Session : {self._session_name}\n")
                f.write(f"Date    : {datetime.datetime.now().strftime('%d/%m/%Y')}\n")
                f.write("─" * 50 + "\n\n")
                for entry in self._log_entries:
                    f.write(entry + "\n")
            print(f"[Record] 📝 Log → {log_path}")
        except Exception as e:
            print(f"[Record] ❌ Erreur log : {e}")

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        s = int(seconds)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ─── Application principale ───────────────────────────────────────────────────
class VideoCallApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("🎥 PolyMeet — Salle Vidéo + Audio")
        self.geometry("1280x860")
        self.configure(fg_color="#0d1117")

        self.ws          = None
        self.loop        = None
        self.net_thread  = None
        self.cap         = None
        self.frame_job   = None
        self.call_active = False
        self.my_name     = ""

        self.cam_sources  = []
        self.cam_index    = 0
        self.cam_source   = None

        # Microphones [F-01]
        self.mic_sources  = []   # liste (device_index, label, rate)
        self.mic_index    = 0    # index courant dans mic_sources

        self.audio_engine:         AudioEngine    | None = None
        self.recording_engine:     RecordingEngine        = RecordingEngine()
        self.transcription_engine                         = None  # [F-03]
        self.diarization_engine                           = None  # [F-04]
        self.translation_engine                           = None  # [F-05]
        self.analysis_engine                              = AnalysisEngine() if ANALYSIS_AVAILABLE else None  # [F-07]
        self._last_analysis                               = None  # dernier résultat

        self.tiles   = {}
        self.my_tile = None

        # Timer d'affichage de la durée [F-02]
        self._clock_job = None

        self._build_ui()
        self.bind("<Configure>", lambda e: self.after(100, self._relayout_grid))

        # ── Raccourcis clavier [F-02] ────────────────────────────────────
        self.bind("<Control-r>", lambda e: self._rec_start())
        self.bind("<Control-p>", lambda e: self._rec_pause_resume())
        self.bind("<Control-s>", lambda e: self._rec_stop())

    # ──────────────────────────────────────────────────────────────────────────
    # Construction de l'interface
    # ──────────────────────────────────────────────────────────────────────────
    def _build_ui(self):
        # ── Barre du haut ─────────────────────────────────────────────────
        topbar = ctk.CTkFrame(self, height=52, fg_color="#161b22", corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        ctk.CTkLabel(topbar, text="🎥 PolyMeet",
                     font=("Arial", 18, "bold")).pack(side="left", padx=18)

        self.status_lbl = ctk.CTkLabel(
            topbar, text="⬤ Déconnecté",
            font=("Arial", 12), text_color="#888")
        self.status_lbl.pack(side="right", padx=18)

        self.members_lbl = ctk.CTkLabel(
            topbar, text="👥 0 participant(s)",
            font=("Arial", 11), text_color="#aaa")
        self.members_lbl.pack(side="right", padx=10)

        # ── Barre de formulaire ───────────────────────────────────────────
        form = ctk.CTkFrame(self, fg_color="#0d1117", corner_radius=0, height=50)
        form.pack(fill="x", side="top")
        form.pack_propagate(False)

        ctk.CTkLabel(form, text="Nom :", width=40,
                     font=("Arial", 11)).pack(side="left", padx=(14, 2))
        self.name_entry = ctk.CTkEntry(
            form, placeholder_text="Votre prénom", width=130, height=32)
        self.name_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(form, text="Salle :", width=40,
                     font=("Arial", 11)).pack(side="left", padx=(0, 2))
        self.room_entry = ctk.CTkEntry(
            form, placeholder_text="general", width=110, height=32)
        self.room_entry.pack(side="left", padx=(0, 10))

        ctk.CTkLabel(form, text="IP :", width=25,
                     font=("Arial", 11)).pack(side="left", padx=(0, 2))
        self.server_entry = ctk.CTkEntry(
            form, placeholder_text=SERVER_IP, width=130, height=32)
        self.server_entry.pack(side="left", padx=(0, 10))

        self.cam_status = ctk.CTkLabel(
            form, text="📷 —", font=("Arial", 10), text_color="#888")
        self.cam_status.pack(side="left", padx=(0, 4))

        self.mic_status = ctk.CTkLabel(
            form, text="🎤 —", font=("Arial", 10), text_color="#888")
        self.mic_status.pack(side="left", padx=(0, 4))

        # Sélecteur de micro [F-01]
        self._mic_var = tk.StringVar(value="— Micro —")
        self.mic_selector = ctk.CTkOptionMenu(
            form, variable=self._mic_var,
            values=["— Micro —"],
            width=180, height=28,
            font=("Arial", 9),
            fg_color="#1a2a3a", button_color="#1e3248",
            command=self._on_mic_selected)
        self.mic_selector.pack(side="left", padx=(0, 6))
        # Peupler la liste au démarrage
        self._populate_mic_selector()

        self.join_btn = ctk.CTkButton(
            form, text="📞 Rejoindre", width=110, height=32,
            fg_color="#1a7a3c", hover_color="#145e2e",
            font=("Arial", 12, "bold"), command=self._join)
        self.join_btn.pack(side="left", padx=(0, 6))

        self.leave_btn = ctk.CTkButton(
            form, text="📵 Quitter", width=90, height=32,
            fg_color="#b03030", hover_color="#8a2020",
            font=("Arial", 12, "bold"),
            command=self._leave, state="disabled")
        self.leave_btn.pack(side="left", padx=(0, 6))

        self.mute_btn = ctk.CTkButton(
            form, text="🎤 Mute", width=84, height=32,
            fg_color="#444", hover_color="#555",
            font=("Arial", 11, "bold"),
            command=self._toggle_mute, state="disabled")
        self.mute_btn.pack(side="left", padx=(0, 6))

        self.cam_btn = ctk.CTkButton(
            form, text="🔄 Caméra", width=94, height=32,
            fg_color="#1a3a6a", hover_color="#142c52",
            font=("Arial", 11, "bold"),
            command=self._switch_camera, state="disabled")
        self.cam_btn.pack(side="left", padx=(0, 6))

        # ── Zone centrale : grille + chat ─────────────────────────────────
        center = ctk.CTkFrame(self, fg_color="transparent")
        center.pack(fill="both", expand=True)

        grid_outer = ctk.CTkFrame(center, fg_color="#0d1117", corner_radius=0)
        grid_outer.pack(side="left", fill="both", expand=True)

        self.grid_canvas = tk.Frame(grid_outer, bg="#0d1117")
        self.grid_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        # ── Panneau droit : onglets Chat + Transcription ──────────────────
        right_panel = ctk.CTkFrame(center, width=290, fg_color="#161b22",
                                   corner_radius=0)
        right_panel.pack(side="right", fill="y")
        right_panel.pack_propagate(False)

        # ── Barre d'onglets ───────────────────────────────────────────────
        tab_bar = ctk.CTkFrame(right_panel, fg_color="#0d1117", height=38,
                               corner_radius=0)
        tab_bar.pack(fill="x", side="top")
        tab_bar.pack_propagate(False)

        self._active_tab   = tk.StringVar(value="chat")

        self.tab_chat_btn  = ctk.CTkButton(
            tab_bar, text="💬 Chat", width=130, height=36,
            fg_color="#1a2a3a", hover_color="#1e3248",
            font=("Arial", 11, "bold"),
            command=lambda: self._switch_tab("chat"))
        self.tab_chat_btn.pack(side="left", padx=(2, 1), pady=1)

        self.tab_trans_btn = ctk.CTkButton(
            tab_bar, text="📝 Transcription", width=145, height=36,
            fg_color="#111820", hover_color="#1e3248",
            font=("Arial", 11),
            command=lambda: self._switch_tab("transcription"))
        self.tab_trans_btn.pack(side="left", padx=(1, 2), pady=1)

        # ── Contenu onglet CHAT ───────────────────────────────────────────
        self.chat_frame = ctk.CTkFrame(right_panel, fg_color="transparent")
        self.chat_frame.pack(fill="both", expand=True)

        self.chat_box = ctk.CTkTextbox(
            self.chat_frame, font=("Arial", 11), wrap="word")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(6, 4))
        self.chat_box.configure(state="disabled")

        chat_row = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        chat_row.pack(fill="x", padx=8, pady=(0, 10))
        self.chat_entry = ctk.CTkEntry(
            chat_row, placeholder_text="Message…", height=32)
        self.chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.chat_entry.bind("<Return>", lambda e: self._send_chat())
        ctk.CTkButton(
            chat_row, text="↩", width=34, height=32,
            command=self._send_chat).pack(side="left")

        # ── Contenu onglet TRANSCRIPTION [F-03] ──────────────────────────
        self.trans_frame = ctk.CTkFrame(right_panel, fg_color="transparent")
        # (caché par défaut — affiché via _switch_tab)

        # ── Sous-panneau Speakers [F-04] ─────────────────────────────────
        self.speakers_panel = ctk.CTkFrame(
            self.trans_frame, fg_color="#0d1520",
            corner_radius=6, border_width=1, border_color="#1e2d45")
        self.speakers_panel.pack(fill="x", padx=8, pady=(6, 4))

        spk_header = ctk.CTkFrame(self.speakers_panel, fg_color="transparent")
        spk_header.pack(fill="x", padx=8, pady=(4, 2))

        ctk.CTkLabel(spk_header, text="👥 Participants",
                     font=("Arial", 10, "bold"),
                     text_color="#4a7abf").pack(side="left")

        self.spk_count_lbl = ctk.CTkLabel(
            spk_header, text="0 speaker(s)",
            font=("Arial", 9), text_color="#555")
        self.spk_count_lbl.pack(side="right")

        # Zone scrollable pour les badges speakers
        self.spk_list_frame = ctk.CTkScrollableFrame(
            self.speakers_panel, fg_color="transparent",
            height=70, scrollbar_button_color="#1e2d45")
        self.spk_list_frame.pack(fill="x", padx=4, pady=(0, 4))

        # Dict pour stocker les widgets badges : auto_name → frame
        self._speaker_badges = {}

        # Status langue détectée
        trans_top = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trans_top.pack(fill="x", padx=8, pady=(2, 2))

        ctk.CTkLabel(trans_top, text="Langue :",
                     font=("Arial", 10), text_color="#555").pack(side="left")
        self.trans_lang_lbl = ctk.CTkLabel(
            trans_top, text="—",
            font=("Arial", 10, "bold"), text_color="#4a9abf")
        self.trans_lang_lbl.pack(side="left", padx=(4, 0))

        self.trans_status_lbl = ctk.CTkLabel(
            trans_top, text="⬤ Inactif",
            font=("Arial", 10), text_color="#444")
        self.trans_status_lbl.pack(side="right")

        # Zone de texte transcrit (éditable = correction en direct)
        self.trans_box = ctk.CTkTextbox(
            self.trans_frame, font=("Arial", 11), wrap="word")
        self.trans_box.pack(fill="both", expand=True, padx=8, pady=(2, 4))
        self.trans_box.insert("end", "La transcription apparaîtra ici…\n")
        self.trans_box.configure(text_color="#555")

        # Boutons actions transcription
        trans_btns = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trans_btns.pack(fill="x", padx=8, pady=(0, 10))

        self.trans_toggle_btn = ctk.CTkButton(
            trans_btns, text="▶ Activer", width=110, height=30,
            fg_color="#1a4a6a", hover_color="#143a54",
            font=("Arial", 10, "bold"),
            command=self._toggle_transcription)
        self.trans_toggle_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            trans_btns, text="💾 Exporter", width=100, height=30,
            fg_color="#2a3a2a", hover_color="#1e2e1e",
            font=("Arial", 10),
            command=self._export_transcript).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            trans_btns, text="🗑", width=34, height=30,
            fg_color="#3a2020", hover_color="#2e1818",
            font=("Arial", 10),
            command=self._clear_transcript).pack(side="left")

        # ══════════════════════════════════════════════════════════════════
        # [F-05/F-06] Sous-panneau Traduction
        # ══════════════════════════════════════════════════════════════════
        trans_divider = ctk.CTkFrame(
            self.trans_frame, height=1, fg_color="#1e2d45")
        trans_divider.pack(fill="x", padx=8, pady=(4, 4))

        trad_header = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trad_header.pack(fill="x", padx=8, pady=(2, 2))

        ctk.CTkLabel(trad_header, text="🌍 Traduction",
                     font=("Arial", 10, "bold"),
                     text_color="#4a9abf").pack(side="left")

        self.trad_status_lbl = ctk.CTkLabel(
            trad_header, text="⬤ Inactif",
            font=("Arial", 9), text_color="#444")
        self.trad_status_lbl.pack(side="right")

        # Sélecteur de langue cible
        trad_lang_row = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trad_lang_row.pack(fill="x", padx=8, pady=(2, 4))

        ctk.CTkLabel(trad_lang_row, text="Langue :",
                     font=("Arial", 10), text_color="#888").pack(side="left", padx=(0,6))

        # Construire les options depuis SUPPORTED_LANGUAGES si disponible
        lang_options = []
        if TRANSLATION_AVAILABLE:
            for code, label, flag in SUPPORTED_LANGUAGES:
                lang_options.append(f"{flag} {label}")
        else:
            lang_options = ["🇫🇷 Français", "🇬🇧 English"]

        self._trad_lang_var = tk.StringVar(value="🇫🇷 Français")
        self.trad_lang_menu = ctk.CTkOptionMenu(
            trad_lang_row,
            variable=self._trad_lang_var,
            values=lang_options,
            width=145, height=26,
            font=("Arial", 9),
            fg_color="#1a2a3a", button_color="#1e3248",
            command=self._on_trad_lang_changed)
        self.trad_lang_menu.pack(side="left", padx=(0, 6))

        self.trad_toggle_btn = ctk.CTkButton(
            trad_lang_row, text="▶ ON", width=58, height=26,
            fg_color="#1a4a6a", hover_color="#143a54",
            font=("Arial", 9, "bold"),
            command=self._toggle_translation)
        self.trad_toggle_btn.pack(side="left")

        # Zone d'affichage de la traduction
        self.trad_box = ctk.CTkTextbox(
            self.trans_frame, font=("Arial", 11), wrap="word", height=120)
        self.trad_box.pack(fill="x", padx=8, pady=(0, 6))
        self.trad_box.insert("end", "La traduction apparaîtra ici…\n")
        self.trad_box.configure(text_color="#555")

        ctk.CTkButton(
            self.trans_frame, text="🗑 Effacer traduction",
            width=140, height=24,
            fg_color="#2a1a1a", hover_color="#3a2020",
            font=("Arial", 9),
            command=self._clear_translation).pack(anchor="e", padx=8, pady=(0, 4))

        # ── Bouton Analyser [F-07/F-08] ───────────────────────────────────
        analyze_row = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        analyze_row.pack(fill="x", padx=8, pady=(4, 8))

        ctk.CTkButton(
            analyze_row,
            text="🧠 Analyser la réunion",
            width=180, height=32,
            fg_color="#2a1a4a", hover_color="#3a2a5a",
            font=("Arial", 10, "bold"),
            command=self._run_analysis
        ).pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            analyze_row,
            text="💾 Export",
            width=70, height=32,
            fg_color="#1a2a1a", hover_color="#2a3a2a",
            font=("Arial", 10),
            command=self._export_analysis
        ).pack(side="left")

        # ══════════════════════════════════════════════════════════════════
        # [F-02] Panneau d'enregistrement (bas de fenêtre)
        # ══════════════════════════════════════════════════════════════════
        self._build_recording_panel()
        # Afficher l'onglet chat par défaut
        self._switch_tab("chat")

    # ──────────────────────────────────────────────────────────────────────────
    # Gestion des onglets Chat / Transcription
    # ──────────────────────────────────────────────────────────────────────────
    def _switch_tab(self, tab: str):
        """Affiche l'onglet demandé et cache l'autre."""
        self._active_tab.set(tab)

        if tab == "chat":
            self.trans_frame.pack_forget()
            self.chat_frame.pack(fill="both", expand=True)
            self.tab_chat_btn.configure(fg_color="#1a2a3a")
            self.tab_trans_btn.configure(fg_color="#111820")
        else:
            self.chat_frame.pack_forget()
            self.trans_frame.pack(fill="both", expand=True)
            self.tab_trans_btn.configure(fg_color="#1a2a3a")
            self.tab_chat_btn.configure(fg_color="#111820")

    # ──────────────────────────────────────────────────────────────────────────
    # [F-03] Méthodes de transcription
    # ──────────────────────────────────────────────────────────────────────────
    def _toggle_transcription(self):
        """Démarre ou arrête le moteur de transcription."""
        if not TRANSCRIPTION_AVAILABLE:
            msgbox.showerror(
                "Module manquant",
                "transcription_engine.py introuvable.\n"
                "Vérifiez que le fichier est dans le même dossier.")
            return

        if self.transcription_engine is None:
            # Vérifier si le modèle est en cache avant de lancer
            engine_check = TranscriptionEngine()
            if not engine_check.is_model_cached():
                model_name = "small"
                reponse = msgbox.askyesno(
                    "Téléchargement requis",
                    f"Le modèle Whisper '{model_name}' n'est pas encore téléchargé.\n\n"
                    f"• Taille : ~460 MB\n"
                    f"• Internet requis une seule fois\n"
                    f"• Ensuite : 100% offline ✅\n\n"
                    f"Cache : {engine_check.get_cache_path()}\n\n"
                    f"Lancer le téléchargement maintenant ?")
                if not reponse:
                    return

            # Démarrer
            self.trans_toggle_btn.configure(text="⏳ Chargement…", state="disabled")
            self.trans_status_lbl.configure(text="⬤ Chargement…", text_color="#e0a030")
            self.update()
            self.transcription_engine = TranscriptionEngine(
                on_segment=self._on_transcript_segment)
            ok = self.transcription_engine.start()
            if ok:
                self.trans_toggle_btn.configure(
                    text="⏹ Désactiver", state="normal",
                    fg_color="#6a1a1a", hover_color="#541414")
                self.trans_status_lbl.configure(
                    text="⬤ Actif", text_color="#4caf50")
                self.trans_box.delete("1.0", "end")
                self.trans_box.configure(text_color="white")
                self._add_chat_line("📝 Transcription activée (Whisper small)")
            else:
                # Récupérer le type d'erreur
                err = getattr(self.transcription_engine, "_error", "")
                self.transcription_engine = None
                self.trans_toggle_btn.configure(
                    text="▶ Activer", state="normal",
                    fg_color="#1a4a6a", hover_color="#143a54")
                self.trans_status_lbl.configure(
                    text="⬤ Erreur", text_color="#e05050")

                if err == "no_internet":
                    msgbox.showerror(
                        "Pas de connexion internet",
                        "Le modèle Whisper doit être téléchargé une première fois.\n\n"
                        "✅ Connectez-vous à internet et réessayez.\n"
                        "✅ Après le 1er téléchargement : fonctionne 100% offline.\n\n"
                        f"Chemin du cache : {TranscriptionEngine.get_cache_path()}")
                else:
                    msgbox.showerror(
                        "Erreur de chargement",
                        f"Impossible de charger le modèle Whisper.\n\nDétail : {err}\n\n"
                        "Vérifiez que openai-whisper est bien installé :\n"
                        "pip install openai-whisper")
        else:
            # Arrêter
            self.transcription_engine.stop()
            self.transcription_engine = None
            self.trans_toggle_btn.configure(
                text="▶ Activer", state="normal",
                fg_color="#1a4a6a", hover_color="#143a54")
            self.trans_status_lbl.configure(text="⬤ Inactif", text_color="#444")
            self._add_chat_line("📝 Transcription désactivée")

    def _on_record_chunk_and_transcribe(self, raw_bytes: bytes):
        """
        Callback unifié : envoie le chunk à RecordingEngine,
        TranscriptionEngine (rééchantillonné à 16000 Hz) et DiarizationEngine.
        """
        self.recording_engine.add_chunk(raw_bytes)
        if self.transcription_engine and self.transcription_engine.is_running:
            # Rééchantillonnage vers 16000 Hz requis par Whisper
            whisper_chunk = (self.audio_engine.resample_to_whisper(raw_bytes)
                             if self.audio_engine else raw_bytes)
            self.transcription_engine.feed(whisper_chunk)
        # Mode présentiel : analyse vocale locale
        if (self.diarization_engine and
                self.diarization_engine._mode == DiarizationEngine.MODE_PRESENTIEL
                if DIARIZATION_AVAILABLE else False):
            self.diarization_engine.feed_local_audio(raw_bytes)

    # ──────────────────────────────────────────────────────────────────────────
    # [F-04] Diarisation — gestion des speakers
    # ──────────────────────────────────────────────────────────────────────────

    def _start_diarization(self, mode: str):
        """Démarre le moteur de diarisation dans le mode indiqué."""
        if not DIARIZATION_AVAILABLE:
            return
        if self.diarization_engine:
            return
        self.diarization_engine = DiarizationEngine(
            on_speaker_change=self._on_speaker_change,
            on_speakers_updated=self._on_speakers_updated)
        self.diarization_engine.start(mode=mode)
        print(f"[Diarization] ▶ Démarré mode {mode}")

    def _stop_diarization(self):
        if self.diarization_engine:
            self.diarization_engine.stop()
            self.diarization_engine = None

    def _on_speaker_change(self, speaker, timestamp: str):
        """
        Callback quand le speaker courant change.
        Appelé depuis thread réseau ou thread diarisation → repasser dans UI.
        """
        def _update():
            if self.transcription_engine:
                self.transcription_engine.set_current_speaker(
                    label=speaker.label,
                    color=speaker.color)
            # Mettre en surbrillance le badge du speaker actif
            self._highlight_active_speaker(speaker.auto_name)
        self.after(0, _update)

    def _on_speakers_updated(self, speakers: dict):
        """
        Callback quand la liste des speakers change.
        Met à jour les badges dans le sous-panneau Speakers.
        """
        def _update():
            self._refresh_speaker_badges(speakers)
        self.after(0, _update)

    def _refresh_speaker_badges(self, speakers: dict):
        """Recrée les badges speakers dans le sous-panneau."""
        # Supprimer les badges obsolètes
        current_keys = set(speakers.keys())
        for key in list(self._speaker_badges.keys()):
            if key not in current_keys:
                self._speaker_badges[key].destroy()
                del self._speaker_badges[key]

        # Ajouter / mettre à jour les badges
        for auto_name, profile in speakers.items():
            if auto_name not in self._speaker_badges:
                self._create_speaker_badge(auto_name, profile)
            else:
                # Mettre à jour le label si renommé
                badge = self._speaker_badges[auto_name]
                try:
                    badge._name_lbl.configure(text=profile.label)
                    badge._count_lbl.configure(
                        text=f"{profile.speech_count} int.")
                except Exception:
                    pass

        count = len(speakers)
        self.spk_count_lbl.configure(
            text=f"{count} speaker{'s' if count > 1 else ''}")

    def _create_speaker_badge(self, auto_name: str, profile):
        """Crée un badge coloré pour un speaker dans le sous-panneau."""
        badge = tk.Frame(self.spk_list_frame, bg="#0d1117",
                         relief="flat", bd=0)
        badge.pack(fill="x", padx=2, pady=2)

        # Pastille colorée
        dot = tk.Label(badge, text="⬤", bg="#0d1117",
                       fg=profile.color, font=("Arial", 10))
        dot.pack(side="left", padx=(4, 2))

        # Nom (cliquable pour renommer)
        name_lbl = tk.Label(badge, text=profile.label,
                            bg="#0d1117", fg="white",
                            font=("Arial", 10, "bold"),
                            cursor="hand2")
        name_lbl.pack(side="left", padx=(0, 6))
        # Double-clic → renommer
        name_lbl.bind("<Double-Button-1>",
                      lambda e, k=auto_name: self._rename_speaker_dialog(k))

        # Compteur d'interventions
        count_lbl = tk.Label(badge, text=f"{profile.speech_count} int.",
                             bg="#0d1117", fg="#555", font=("Arial", 9))
        count_lbl.pack(side="left")

        # Stocker les refs pour mise à jour ultérieure
        badge._name_lbl  = name_lbl
        badge._count_lbl = count_lbl
        badge._dot       = dot
        badge._is_active = False

        self._speaker_badges[auto_name] = badge

    def _highlight_active_speaker(self, auto_name: str):
        """Met en surbrillance le badge du speaker qui parle actuellement."""
        for key, badge in self._speaker_badges.items():
            is_active = (key == auto_name)
            try:
                bg = "#0d2030" if is_active else "#0d1117"
                badge.configure(bg=bg)
                badge._name_lbl.configure(bg=bg)
                badge._count_lbl.configure(bg=bg)
                badge._dot.configure(bg=bg)
            except Exception:
                pass

    def _rename_speaker_dialog(self, auto_name: str):
        """Ouvre une mini-fenêtre de renommage pour un speaker."""
        if not self.diarization_engine:
            return

        # Fenêtre popup
        dlg = tk.Toplevel(self)
        dlg.title("Renommer le participant")
        dlg.geometry("320x130")
        dlg.configure(bg="#0d1117")
        dlg.resizable(False, False)
        dlg.grab_set()  # modal

        tk.Label(dlg, text=f"Nouveau nom pour « {auto_name} » :",
                 bg="#0d1117", fg="white",
                 font=("Arial", 11)).pack(pady=(16, 6))

        entry = ctk.CTkEntry(dlg, placeholder_text="Entrez un nom…",
                             width=200, height=32)
        entry.pack()
        entry.focus()

        def _confirm():
            new_name = entry.get().strip()
            if new_name:
                self.diarization_engine.rename_speaker(auto_name, new_name)
            dlg.destroy()

        entry.bind("<Return>", lambda e: _confirm())

        btn_row = ctk.CTkFrame(dlg, fg_color="transparent")
        btn_row.pack(pady=10)
        ctk.CTkButton(btn_row, text="✅ Confirmer", width=110, height=28,
                      command=_confirm).pack(side="left", padx=6)
        ctk.CTkButton(btn_row, text="Annuler", width=80, height=28,
                      fg_color="#333", hover_color="#444",
                      command=dlg.destroy).pack(side="left")

    def _export_transcript(self):
        """Exporte la transcription complète dans un fichier .txt"""
        if self.transcription_engine is None:
            msgbox.showinfo("Export", "Aucune transcription active.")
            return
        text = self.transcription_engine.get_full_transcript()
        if not text:
            # Exporter ce qui est dans la zone texte (corrections manuelles)
            text = self.trans_box.get("1.0", "end").strip()
        if not text:
            msgbox.showinfo("Export", "Aucun texte à exporter.")
            return
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RECORDINGS_DIR, f"transcription_{ts}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("PolyMeet — Transcription automatique\n")
                f.write(f"Date : {datetime.datetime.now().strftime('%d/%m/%Y %H:%M')}\n")
                f.write("─" * 50 + "\n\n")
                f.write(text)
            msgbox.showinfo("Export réussi", f"Transcription exportée :\n{path}")
            self._add_chat_line(f"💾 Transcription exportée → {os.path.basename(path)}")
        except Exception as e:
            msgbox.showerror("Erreur", f"Impossible d'exporter : {e}")

    def _clear_transcript(self):
        """Efface la zone de transcription."""
        if msgbox.askyesno("Effacer", "Effacer toute la transcription ?"):
            self.trans_box.delete("1.0", "end")
            if self.transcription_engine:
                self.transcription_engine.clear_transcript()
            self.trans_lang_lbl.configure(text="—")

    # ──────────────────────────────────────────────────────────────────────────
    # [F-05/F-06] Traduction Multilingue
    # ──────────────────────────────────────────────────────────────────────────

    def _get_target_lang_code(self) -> str:
        """Retourne le code ISO de la langue cible sélectionnée."""
        if not TRANSLATION_AVAILABLE:
            return "fr"
        label = self._trad_lang_var.get()
        for code, name, flag in SUPPORTED_LANGUAGES:
            if name in label or flag in label:
                return code
        return "fr"

    def _on_trad_lang_changed(self, choice: str):
        """Appelé quand l'utilisateur change la langue cible."""
        lang_code = self._get_target_lang_code()

        # Mettre à jour le menu pour exclure la langue source actuelle
        if TRANSLATION_AVAILABLE and self.translation_engine:
            src = self.translation_engine.source_language
            available_targets = self.translation_engine.get_available_targets(src)
            new_options = [f"{flag} {name}" for code, name, flag in available_targets]
            self.trad_lang_menu.configure(values=new_options)

            self.trad_status_lbl.configure(
                text="⬤ Chargement…", text_color="#e0a030")
            self.translation_engine.set_target_language(lang_code)

        lang_label = LANG_NAMES.get(lang_code, lang_code.upper()) if TRANSLATION_AVAILABLE else lang_code
        print(f"[Translation] Langue cible changée → {lang_label}")

    def _toggle_translation(self):
        """Active ou désactive la traduction."""
        if not TRANSLATION_AVAILABLE:
            msgbox.showerror("Module manquant",
                             "translation_engine.py introuvable.\n"
                             "pip install argos-translate")
            return

        if self.translation_engine is None:
            lang_code = self._get_target_lang_code()
            self.trad_toggle_btn.configure(text="⏳", state="disabled")
            self.trad_status_lbl.configure(
                text="⬤ Démarrage…", text_color="#e0a030")

            self.translation_engine = TranslationEngine(
                on_translated=self._on_translated,
                on_pack_ready=self._on_translation_pack_ready)
            # Récupérer la langue source détectée par Whisper (si disponible)
            detected_src = "auto"
            if self.transcription_engine and hasattr(self.transcription_engine, "_last_language"):
                detected_src = self.transcription_engine._last_language or "auto"

            ok = self.translation_engine.start(source_lang=detected_src, target_lang=lang_code)
            if ok:
                self.trad_box.delete("1.0", "end")
                self.trad_box.configure(text_color="white")
                self._add_chat_line(f"🌍 Traduction activée → {lang_code.upper()}")
            else:
                self.translation_engine = None
                self.trad_toggle_btn.configure(
                    text="▶ ON", state="normal",
                    fg_color="#1a4a6a", hover_color="#143a54")
                self.trad_status_lbl.configure(text="⬤ Erreur", text_color="#e05050")
                msgbox.showerror("Erreur",
                                 "Impossible de démarrer la traduction.\n"
                                 "Vérifiez : pip install argos-translate")
        else:
            self.translation_engine.stop()
            self.translation_engine = None
            self.trad_toggle_btn.configure(
                text="▶ ON", state="normal",
                fg_color="#1a4a6a", hover_color="#143a54")
            self.trad_status_lbl.configure(text="⬤ Inactif", text_color="#444")
            self._add_chat_line("🌍 Traduction désactivée")

    def _on_translation_pack_ready(self, lang_code: str):
        """Callback quand le pack de langue est prêt (thread loader → UI)."""
        def _update():
            label = LANG_NAMES.get(lang_code, lang_code.upper())
            self.trad_status_lbl.configure(
                text=f"⬤ Actif ({label})", text_color="#4caf50")
            self.trad_toggle_btn.configure(
                text="⏹ OFF", state="normal",
                fg_color="#6a1a1a", hover_color="#541414")
            if lang_code == "mg":
                self.trad_box.insert("end",
                    "ℹ Malagasy : texte original affiché (traduction non disponible offline)\n")
        self.after(0, _update)

    def _on_translated(self, original: str, translated: str,
                       source_lang: str, target_lang: str):
        """
        Callback appelé par TranslationEngine quand une traduction est prête.
        Thread translation → thread UI via after().
        """
        def _update():
            # Afficher dans la zone de traduction
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {translated}\n"

            self.trad_box.configure(state="normal")
            self.trad_box.insert("end", line)
            self.trad_box.see("end")

            # Notif onglet si on est sur Chat
            if self._active_tab.get() == "chat":
                self.tab_trans_btn.configure(text="📝 Transcription ●")
        self.after(0, _update)

    def _clear_translation(self):
        """Efface la zone de traduction."""
        self.trad_box.delete("1.0", "end")

    # ──────────────────────────────────────────────────────────────────────────
    # [F-07/F-08] Analyse IA & Résumé Intelligent
    # ──────────────────────────────────────────────────────────────────────────

    def _run_analysis(self):
        """Lance l'analyse de la transcription courante."""
        if not ANALYSIS_AVAILABLE:
            msgbox.showerror("Module manquant",
                             "analysis_engine.py introuvable.")
            return

        # Récupérer la transcription
        transcript = ""
        if self.transcription_engine:
            transcript = self.transcription_engine.get_full_transcript()
        if not transcript:
            transcript = self.trans_box.get("1.0", "end").strip()

        if not transcript or len(transcript) < 20:
            msgbox.showwarning("Transcription vide",
                               "Il faut d'abord transcrire la réunion.\n"
                               "Activez la transcription et parlez quelques minutes.")
            return

        # Récupérer les speakers
        speakers = list(self.tiles.keys()) if self.tiles else []

        # Lancer l'analyse dans un thread (non-bloquant)
        self.trans_box.configure(state="normal")
        self.trans_box.insert("end", "\n⏳ Analyse en cours…\n")
        self.trans_box.see("end")

        def _do_analysis():
            analysis = self.analysis_engine.analyze(transcript, speakers=speakers)
            self._last_analysis = analysis
            self.after(0, lambda: self._show_analysis_result(analysis))

        threading.Thread(target=_do_analysis, daemon=True).start()

    def _show_analysis_result(self, analysis):
        """Affiche le résultat de l'analyse dans l'UI."""
        # Ouvrir une fenêtre de résultat dédiée
        win = tk.Toplevel(self)
        win.title("🧠 Analyse de la réunion — PolyMeet")
        win.geometry("720x600")
        win.configure(bg="#0d1117")

        # Onglets dans la fenêtre
        tab_bar = ctk.CTkFrame(win, fg_color="#161b22", height=40, corner_radius=0)
        tab_bar.pack(fill="x", side="top")
        tab_bar.pack_propagate(False)

        content = ctk.CTkFrame(win, fg_color="#0d1117", corner_radius=0)
        content.pack(fill="both", expand=True)

        text_area = ctk.CTkTextbox(content, font=("Courier", 10), wrap="word")
        text_area.pack(fill="both", expand=True, padx=8, pady=8)

        # Afficher le rapport formaté
        report = self.analysis_engine.format_analysis_report(analysis)
        text_area.insert("end", report)
        text_area.configure(state="disabled")

        # Bouton copier
        btn_row = ctk.CTkFrame(win, fg_color="#161b22", height=44, corner_radius=0)
        btn_row.pack(fill="x", side="bottom")
        btn_row.pack_propagate(False)

        def _copy():
            win.clipboard_clear()
            win.clipboard_append(report)
            msgbox.showinfo("Copié", "Rapport copié dans le presse-papiers !")

        ctk.CTkButton(btn_row, text="📋 Copier le rapport",
                      width=160, height=30, command=_copy).pack(
            side="left", padx=10, pady=7)

        ctk.CTkButton(btn_row, text="💾 Exporter .txt",
                      width=140, height=30,
                      fg_color="#2a3a2a", hover_color="#1e2e1e",
                      command=lambda: self._export_analysis(report)).pack(
            side="left", padx=4, pady=7)

        ctk.CTkButton(btn_row, text="✕ Fermer",
                      width=80, height=30,
                      fg_color="#3a2020", hover_color="#2e1818",
                      command=win.destroy).pack(side="right", padx=10, pady=7)

        # Notifier dans le chat
        self._add_chat_line(
            f"🧠 Analyse terminée — "
            f"{len(analysis.decisions)} décision(s), "
            f"{len(analysis.tasks)} tâche(s), "
            f"sentiment : {analysis.sentiment}")

    def _export_analysis(self, report: str = ""):
        """Exporte le rapport d'analyse en fichier .txt"""
        if not report and self._last_analysis:
            report = self.analysis_engine.format_analysis_report(
                self._last_analysis)
        if not report:
            msgbox.showwarning("Aucune analyse",
                               "Lancez d'abord une analyse avec 🧠 Analyser.")
            return
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RECORDINGS_DIR, f"analyse_{ts}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
            msgbox.showinfo("Export réussi",
                            f"Rapport exporté :\n{path}")
            self._add_chat_line(
                f"💾 Analyse exportée → {os.path.basename(path)}")
        except Exception as e:
            msgbox.showerror("Erreur", f"Export impossible : {e}")

    def _on_transcript_segment(self, text: str, language: str, timestamp: str,
                               speaker_label: str = "?", speaker_color: str = "#aaa"):
        """
        Callback appelé par TranscriptionEngine.
        Affiche le segment ET le traduit si la traduction est active.
        """
        def _update():
            lang_names = {
                "fr": "Français 🇫🇷", "en": "English 🇬🇧",
                "mg": "Malagasy 🇲🇬", "ar": "Arabe 🇸🇦",
                "es": "Espagnol 🇪🇸", "de": "Allemand 🇩🇪",
                "zh": "Chinois 🇨🇳", "pt": "Portugais 🇵🇹",
                "it": "Italien 🇮🇹",
            }
            lang_display = lang_names.get(language, language.upper())
            self.trans_lang_lbl.configure(text=lang_display)

            # Affichage avec couleur speaker
            tag = f"spk_{speaker_label.replace(' ', '_')}"
            try:
                self.trans_box._textbox.tag_configure(tag, foreground=speaker_color)
            except Exception:
                pass

            self.trans_box.configure(state="normal")
            self.trans_box._textbox.insert("end", f"[{timestamp}] ", "timestamp")
            self.trans_box._textbox.insert("end", f"{speaker_label}: ", tag)
            self.trans_box._textbox.insert("end", f"{text}\n")
            self.trans_box._textbox.tag_configure("timestamp", foreground="#555")
            self.trans_box.see("end")

            if self._active_tab.get() == "chat":
                self.tab_trans_btn.configure(text="📝 Transcription ●")

            # ── Envoyer à la traduction si active ────────────────────────
            if self.translation_engine and self.translation_engine.is_ready:
                # Informer le moteur de la langue détectée par Whisper
                self.translation_engine.set_source_language(language)
                # Ne pas traduire si source == cible
                if language != self.translation_engine.target_language:
                    self.translation_engine.translate(text, source_lang=language)

        self.after(0, _update)

    def _build_recording_panel(self):

        """Construit le panneau F-02 Contrôle de l'Enregistrement."""

        rec_panel = ctk.CTkFrame(
            self, height=62, fg_color="#0e1520",
            corner_radius=0,
            border_width=1, border_color="#1e2d45")
        rec_panel.pack(fill="x", side="bottom")
        rec_panel.pack_propagate(False)

        # ── Étiquette section ────────────────────────────────────────────
        ctk.CTkLabel(
            rec_panel, text="⏺  ENREGISTREMENT",
            font=("Arial", 10, "bold"), text_color="#4a7abf"
        ).pack(side="left", padx=(14, 10))

        # ── Bouton START ─────────────────────────────────────────────────
        self.rec_start_btn = ctk.CTkButton(
            rec_panel,
            text="▶  Démarrer", width=118, height=36,
            fg_color="#1a6b3a", hover_color="#145430",
            font=("Arial", 11, "bold"),
            command=self._rec_start)
        self.rec_start_btn.pack(side="left", padx=(0, 6), pady=12)

        # ── Bouton PAUSE / REPRENDRE ─────────────────────────────────────
        self.rec_pause_btn = ctk.CTkButton(
            rec_panel,
            text="⏸  Pause", width=110, height=36,
            fg_color="#555", hover_color="#666",
            font=("Arial", 11, "bold"),
            command=self._rec_pause_resume,
            state="disabled")
        self.rec_pause_btn.pack(side="left", padx=(0, 6), pady=12)

        # ── Bouton STOP ──────────────────────────────────────────────────
        self.rec_stop_btn = ctk.CTkButton(
            rec_panel,
            text="⏹  Arrêter", width=110, height=36,
            fg_color="#7a2020", hover_color="#5e1818",
            font=("Arial", 11, "bold"),
            command=self._rec_stop,
            state="disabled")
        self.rec_stop_btn.pack(side="left", padx=(0, 14), pady=12)

        # ── Séparateur ───────────────────────────────────────────────────
        sep = ctk.CTkFrame(rec_panel, width=2, height=36, fg_color="#1e2d45")
        sep.pack(side="left", padx=(0, 14))

        # ── Indicateur statut ────────────────────────────────────────────
        self.rec_status_dot = ctk.CTkLabel(
            rec_panel, text="⬤",
            font=("Arial", 14), text_color="#333")
        self.rec_status_dot.pack(side="left", padx=(0, 4))

        self.rec_status_lbl = ctk.CTkLabel(
            rec_panel, text="Inactif",
            font=("Arial", 11), text_color="#555")
        self.rec_status_lbl.pack(side="left", padx=(0, 16))

        # ── Durée en temps réel ──────────────────────────────────────────
        ctk.CTkLabel(
            rec_panel, text="⏱",
            font=("Arial", 13)).pack(side="left", padx=(0, 4))

        self.rec_duration_lbl = ctk.CTkLabel(
            rec_panel, text="00:00",
            font=("Arial", 14, "bold"), text_color="#4a7abf")
        self.rec_duration_lbl.pack(side="left", padx=(0, 16))

        # ── Autosave indicator ───────────────────────────────────────────
        self.rec_autosave_lbl = ctk.CTkLabel(
            rec_panel, text="",
            font=("Arial", 10), text_color="#4a9a4a")
        self.rec_autosave_lbl.pack(side="left", padx=(0, 10))

        # ── Raccourcis hint ──────────────────────────────────────────────
        ctk.CTkLabel(
            rec_panel,
            text="Ctrl+R  Démarrer   Ctrl+P  Pause   Ctrl+S  Arrêter",
            font=("Arial", 9), text_color="#333"
        ).pack(side="right", padx=14)

    # ──────────────────────────────────────────────────────────────────────────
    # [F-02] Actions d'enregistrement
    # ──────────────────────────────────────────────────────────────────────────
    def _rec_start(self):
        if self.recording_engine.state != RecordingEngine.STATE_IDLE:
            return
        name = self.my_name or "session"
        ts   = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        ok   = self.recording_engine.start(f"{name}_{ts}")
        if ok:
            self._update_rec_ui()
            self._start_clock()
            self._add_chat_line("⏺ Enregistrement démarré")

    def _rec_pause_resume(self):
        re = self.recording_engine
        if re.state == RecordingEngine.STATE_RECORDING:
            re.pause()
            self._add_chat_line("⏸ Enregistrement mis en pause")
        elif re.state == RecordingEngine.STATE_PAUSED:
            re.resume()
            self._add_chat_line("▶ Enregistrement repris")
        else:
            return
        self._update_rec_ui()

    def _rec_stop(self):
        if self.recording_engine.state == RecordingEngine.STATE_IDLE:
            return
        path = self.recording_engine.stop()
        self._stop_clock()
        self._update_rec_ui()
        if path:
            self._add_chat_line(f"⏹ Enregistrement sauvegardé → {os.path.basename(path)}")
            msgbox.showinfo(
                "Enregistrement sauvegardé",
                f"Fichier enregistré dans :\n{path}\n\n"
                f"Le journal d'horodatage a également été sauvegardé.")
        else:
            self._add_chat_line("⏹ Enregistrement arrêté (aucun audio capturé)")

    # ── Mise à jour visuelle du panneau ─────────────────────────────────────
    def _update_rec_ui(self):
        state = self.recording_engine.state

        if state == RecordingEngine.STATE_IDLE:
            self.rec_status_dot.configure(text_color="#333")
            self.rec_status_lbl.configure(text="Inactif", text_color="#555")
            self.rec_start_btn.configure(state="normal",  text="▶  Démarrer",
                                         fg_color="#1a6b3a", hover_color="#145430")
            self.rec_pause_btn.configure(state="disabled", text="⏸  Pause",
                                         fg_color="#555")
            self.rec_stop_btn.configure(state="disabled")
            self.rec_duration_lbl.configure(text="00:00", text_color="#4a7abf")

        elif state == RecordingEngine.STATE_RECORDING:
            self.rec_status_dot.configure(text_color="#e05050")   # rouge vif
            self.rec_status_lbl.configure(text="En cours…", text_color="#e05050")
            self.rec_start_btn.configure(state="disabled", text="▶  Démarrer",
                                         fg_color="#333", hover_color="#333")
            self.rec_pause_btn.configure(state="normal",  text="⏸  Pause",
                                         fg_color="#555", hover_color="#666")
            self.rec_stop_btn.configure(state="normal")

        elif state == RecordingEngine.STATE_PAUSED:
            self.rec_status_dot.configure(text_color="#e0a030")   # orange
            self.rec_status_lbl.configure(text="En pause", text_color="#e0a030")
            self.rec_pause_btn.configure(state="normal",  text="▶  Reprendre",
                                         fg_color="#1a5a6a", hover_color="#144a56")
            self.rec_stop_btn.configure(state="normal")

    # ── Horloge temps réel ───────────────────────────────────────────────────
    def _start_clock(self):
        self._tick_clock()

    def _stop_clock(self):
        if self._clock_job:
            self.after_cancel(self._clock_job)
            self._clock_job = None

    def _tick_clock(self):
        re = self.recording_engine
        if re.state == RecordingEngine.STATE_IDLE:
            return
        elapsed = re.elapsed_seconds
        self.rec_duration_lbl.configure(
            text=RecordingEngine._fmt_duration(elapsed))

        # Clignotement du point rouge quand en cours
        if re.state == RecordingEngine.STATE_RECORDING:
            current = self.rec_status_dot.cget("text_color")
            next_c  = "#e05050" if current == "#333" else "#333"
            # blink toutes les 1 s
            if int(elapsed) % 2 == 0:
                self.rec_status_dot.configure(text_color="#e05050")
            else:
                self.rec_status_dot.configure(text_color="#7a2020")

        self._clock_job = self.after(1000, self._tick_clock)

    # ──────────────────────────────────────────────────────────────────────────
    # Grille dynamique
    # ──────────────────────────────────────────────────────────────────────────
    def _relayout_grid(self):
        n = len(self.tiles)
        if n == 0:
            return
        cw = self.grid_canvas.winfo_width()
        ch = self.grid_canvas.winfo_height()
        if cw < 20 or ch < 20:
            self.after(200, self._relayout_grid)
            return
        cols = max(1, math.ceil(math.sqrt(n)))
        rows = math.ceil(n / cols)
        gap  = 4
        tw   = max(100, (cw - (cols + 1) * gap) // cols)
        th   = max(80,  (ch - (rows + 1) * gap) // rows)
        for idx, tile in enumerate(self.tiles.values()):
            row = idx // cols; col = idx % cols
            x = gap + col * (tw + gap); y = gap + row * (th + gap)
            tile.place(x=x, y=y, width=tw, height=th)

    def _add_tile(self, name, is_me=False):
        if name in self.tiles:
            return self.tiles[name]
        tile = VideoTile(self.grid_canvas, name, is_me=is_me)
        self.tiles[name] = tile
        self.after(150, self._relayout_grid)
        return tile

    def _remove_tile(self, name):
        if name in self.tiles:
            self.tiles[name].destroy()
            del self.tiles[name]
            self.after(150, self._relayout_grid)
        if self.audio_engine:
            self.audio_engine.remove_participant(name)

    # ──────────────────────────────────────────────────────────────────────────
    # Connexion
    # ──────────────────────────────────────────────────────────────────────────
    def _join(self):
        name   = self.name_entry.get().strip() or "Anonyme"
        room   = self.room_entry.get().strip()  or "general"
        srv_ip = self.server_entry.get().strip() or SERVER_IP
        uri    = f"ws://{srv_ip}:{SERVER_PORT}"
        self.my_name = name

        self.cam_status.configure(text="📷 Recherche…", text_color="#aaa")
        self.update()
        self.cam_sources = list_all_camera_sources()
        self.cam_index   = 0

        if not self.cam_sources:
            self.cam_source = None
            self.cam_status.configure(text="📷 Aucune caméra", text_color="#e07050")
        else:
            self.cam_source = self.cam_sources[0][0]
            lbl = self.cam_sources[0][1]
            self.cam_status.configure(text=f"📷 {lbl}", text_color="#7ec88a")
            if len(self.cam_sources) > 1:
                self.cam_btn.configure(state="normal")

        # AudioEngine avec micro sélectionné [F-01/F-02/F-03]
        dev_idx, dev_rate = self._get_selected_mic()
        print(f"[Mic] Utilisation : index={dev_idx}, taux={dev_rate} Hz")
        self.audio_engine = AudioEngine(
            on_chunk_ready=self._on_audio_chunk_ready,
            on_record_chunk=self._on_record_chunk_and_transcribe,
            device_index=dev_idx,
            device_rate=dev_rate)
        ok = self.audio_engine.start()
        if ok:
            self.mic_status.configure(text=f"🎤 {dev_rate} Hz ✅", text_color="#4caf50")
            self.mute_btn.configure(state="normal")
        else:
            self.mic_status.configure(text="🎤 Erreur", text_color="#e07050")

        self.my_tile = self._add_tile(name, is_me=True)

        # Démarrer la diarisation en mode réseau [F-04]
        self._start_diarization(
            mode=DiarizationEngine.MODE_NETWORK if DIARIZATION_AVAILABLE else "network")
        # Se pré-enregistrer soi-même comme speaker
        if self.diarization_engine:
            self.diarization_engine.add_network_participant(name)

        self.loop = asyncio.new_event_loop()
        self.net_thread = threading.Thread(
            target=self._run_network, args=(uri, name, room), daemon=True)
        self.net_thread.start()
        self.join_btn.configure(state="disabled")
        self.leave_btn.configure(state="normal")

    def _run_network(self, uri, name, room):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect(uri, name, room))

    async def _connect(self, uri, name, room):
        try:
            async with websockets.connect(
                uri, max_size=10_000_000,
                ping_interval=20, ping_timeout=30
            ) as ws:
                self.ws = ws
                await ws.send(json.dumps({"type": "join", "name": name, "room": room}))
                self.after(0, self._start_capture)
                async for raw in ws:
                    self._on_message(raw)
        except Exception as e:
            self.after(0, lambda err=str(e): self._on_disconnect(err))

    # ──────────────────────────────────────────────────────────────────────────
    # Audio callbacks
    # ──────────────────────────────────────────────────────────────────────────
    def _on_audio_chunk_ready(self, b64_chunk: str):
        if self.ws and self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.ws.send(json.dumps({"type": "audio", "chunk": b64_chunk})),
                self.loop)

    # ──────────────────────────────────────────────────────────────────────────
    # [F-01] Sélecteur de microphone
    # ──────────────────────────────────────────────────────────────────────────
    def _populate_mic_selector(self):
        """Détecte les micros disponibles et peuple le menu déroulant."""
        self.mic_sources = list_microphones()
        if not self.mic_sources:
            self.mic_selector.configure(values=["❌ Aucun micro détecté"])
            self._mic_var.set("❌ Aucun micro détecté")
            return
        labels = [lbl for (_, lbl, _) in self.mic_sources]
        self.mic_selector.configure(values=labels)
        # Sélectionner le micro par défaut
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            default_idx = pa.get_default_input_device_info()["index"]
            pa.terminate()
            for i, (dev_idx, lbl, rate) in enumerate(self.mic_sources):
                if dev_idx == default_idx:
                    self.mic_index = i
                    self._mic_var.set(lbl)
                    return
        except Exception:
            pass
        self.mic_index = 0
        self._mic_var.set(labels[0])

    def _on_mic_selected(self, label: str):
        """Appelé quand l'utilisateur choisit un micro dans le dropdown."""
        for i, (dev_idx, lbl, rate) in enumerate(self.mic_sources):
            if lbl == label:
                self.mic_index = i
                self.mic_status.configure(
                    text=f"🎤 {rate} Hz", text_color="#7ec88a")
                print(f"[Mic] Sélectionné : {lbl}")
                break

    def _get_selected_mic(self):
        """Retourne (device_index, rate) du micro actuellement sélectionné."""
        if not self.mic_sources or self.mic_index >= len(self.mic_sources):
            return None, AUDIO_RATE
        dev_idx, lbl, rate = self.mic_sources[self.mic_index]
        return dev_idx, rate

    def _toggle_mute(self):
        if not self.audio_engine:
            return
        muted = self.audio_engine.toggle_mute()
        if muted:
            self.mute_btn.configure(text="🔇 Unmute",
                                    fg_color="#b03030", hover_color="#8a2020")
            self.mic_status.configure(text="🎤 Muté", text_color="#e07050")
        else:
            self.mute_btn.configure(text="🎤 Mute",
                                    fg_color="#444", hover_color="#555")
            self.mic_status.configure(text="🎤 Actif ✅", text_color="#4caf50")

    def _switch_camera(self):
        if len(self.cam_sources) < 2:
            return
        if self.frame_job:
            self.after_cancel(self.frame_job); self.frame_job = None
        if self.cap:
            self.cap.release(); self.cap = None
        self.cam_index = (self.cam_index + 1) % len(self.cam_sources)
        self.cam_source, lbl = self.cam_sources[self.cam_index]
        self.cam_status.configure(text=f"📷 {lbl}", text_color="#7ec88a")
        self._add_chat_line(f"🔄 Caméra : {lbl}")
        if self.call_active:
            self._start_capture()

    # ──────────────────────────────────────────────────────────────────────────
    # Messages WebSocket
    # ──────────────────────────────────────────────────────────────────────────
    def _on_message(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        t = data.get("type")

        if t == "joined":
            self.after(0, lambda d=data: self._on_joined(d))
        elif t == "user_joined":
            n = data["name"]
            self.after(0, lambda n=n: self._add_tile(n))
            self.after(0, lambda n=n: self._add_chat_line(f"✅ {n} a rejoint"))
            self.after(0, lambda n=n: self.recording_engine.log_intervention(n, "A rejoint la réunion"))
            # [F-04] Enregistrer le nouveau participant
            if self.diarization_engine:
                self.after(0, lambda n=n: self.diarization_engine.add_network_participant(n))
        elif t == "user_left":
            n = data["name"]
            self.after(0, lambda n=n: self._remove_tile(n))
            self.after(0, lambda n=n: self._add_chat_line(f"👋 {n} a quitté"))
            self.after(0, lambda n=n: self.recording_engine.log_intervention(n, "A quitté la réunion"))
            # [F-04] Retirer le participant
            if self.diarization_engine:
                self.after(0, lambda n=n: self.diarization_engine.remove_network_participant(n))
        elif t == "members":
            members = data["members"]
            self.after(0, lambda m=members: self._update_members(m))
        elif t == "video":
            name = data["name"]; frame = data["frame"]
            self.after(0, lambda n=name, f=frame: self._show_remote_frame(n, f))
        elif t == "audio":
            name = data["name"]; chunk = data.get("chunk", "")
            if chunk and self.audio_engine:
                self.audio_engine.receive_chunk(name, chunk)
                self.after(0, lambda n=name: self._set_speaking(n, True))
                self.after(400, lambda n=name: self._set_speaking(n, False))
                self.recording_engine.log_intervention(name, "Prise de parole")
                # [F-04] Signaler que ce participant parle (mode réseau)
                if self.diarization_engine:
                    self.diarization_engine.feed_network_speaker(name)
        elif t == "chat":
            line = (f"[{data.get('time', '')}] "
                    f"{data.get('name', '?')} : {data.get('text', '')}")
            self.after(0, lambda l=line: self._add_chat_line(l))
            # Horodatage message chat [F-02]
            self.recording_engine.log_intervention(
                data.get("name", "?"),
                f"Message : {data.get('text', '')[:60]}")

    def _set_speaking(self, name, speaking):
        tile = self.tiles.get(name)
        if tile and tile.winfo_exists():
            tile.set_speaking(speaking)

    def _on_joined(self, data):
        self.leave_btn.configure(state="normal")
        self.status_lbl.configure(
            text=f"⬤ Connecté – {data['room']}", text_color="#4caf50")
        self._add_chat_line(f"✅ Vous avez rejoint « {data['room']} »")

    def _on_disconnect(self, reason=""):
        self.call_active = False
        self.ws = None
        self.status_lbl.configure(text="⬤ Déconnecté", text_color="#e07050")
        self.join_btn.configure(state="normal")
        self.leave_btn.configure(state="disabled")
        self.mute_btn.configure(state="disabled")
        self.cam_btn.configure(state="disabled")
        self._stop_capture()
        self._stop_audio()
        if reason:
            self._add_chat_line(f"⚠ Déconnecté : {reason}")

    def _leave(self):
        self.call_active = False
        self._stop_capture()
        self._stop_audio()
        # Arrêt automatique de l'enregistrement si actif [F-02]
        if self.recording_engine.state != RecordingEngine.STATE_IDLE:
            self._rec_stop()
        # Arrêt automatique de la transcription si active [F-03]
        if self.transcription_engine is not None:
            self.transcription_engine.stop()
            self.transcription_engine = None
            self.trans_toggle_btn.configure(text="▶ Activer", fg_color="#1a4a6a")
            self.trans_status_lbl.configure(text="⬤ Inactif", text_color="#444")
        # Arrêt automatique de la diarisation [F-04]
        self._stop_diarization()
        # Vider les badges speakers
        for badge in list(self._speaker_badges.values()):
            badge.destroy()
        self._speaker_badges.clear()
        self.spk_count_lbl.configure(text="0 speaker(s)")
        # Arrêt automatique de la traduction [F-05]
        if self.translation_engine is not None:
            self.translation_engine.stop()
            self.translation_engine = None
            self.trad_toggle_btn.configure(text="▶ ON", fg_color="#1a4a6a")
            self.trad_status_lbl.configure(text="⬤ Inactif", text_color="#444")

        if self.ws and self.loop:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        for tile in list(self.tiles.values()):
            tile.destroy()
        self.tiles.clear()
        self.my_tile = None
        self.cam_sources = []
        self.status_lbl.configure(text="⬤ Déconnecté", text_color="#888")
        self.members_lbl.configure(text="👥 0 participant(s)")
        self.mic_status.configure(text="🎤 —", text_color="#888")
        self.cam_status.configure(text="📷 —", text_color="#888")
        self.join_btn.configure(state="normal")
        self.leave_btn.configure(state="disabled")
        self.mute_btn.configure(state="disabled", text="🎤 Mute", fg_color="#444")
        self.cam_btn.configure(state="disabled")

    # ──────────────────────────────────────────────────────────────────────────
    # Capture vidéo
    # ──────────────────────────────────────────────────────────────────────────
    def _start_capture(self):
        if self.cam_source is None:
            return
        self.cap = cv2.VideoCapture(self.cam_source)
        if not self.cap.isOpened():
            self.cam_status.configure(text="📷 Erreur", text_color="#e07050")
            return
        self.call_active = True
        self._send_frame()

    def _stop_capture(self):
        if self.frame_job:
            self.after_cancel(self.frame_job); self.frame_job = None
        if self.cap:
            self.cap.release(); self.cap = None

    def _stop_audio(self):
        if self.audio_engine:
            self.audio_engine.stop(); self.audio_engine = None

    def _send_frame(self):
        if not self.call_active or self.cap is None:
            return
        ret, frame = self.cap.read()
        if ret and frame is not None:
            if self.my_tile and self.my_tile.winfo_exists():
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                self.my_tile.update_frame(Image.fromarray(rgb))
            small = cv2.resize(frame, FRAME_RESIZE)
            ok, buf = cv2.imencode(
                ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, FRAME_QUALITY])
            if ok and self.ws and self.loop and not self.loop.is_closed():
                b64 = base64.b64encode(buf.tobytes()).decode()
                asyncio.run_coroutine_threadsafe(
                    self.ws.send(json.dumps({"type": "video", "frame": b64})),
                    self.loop)
        self.frame_job = self.after(FRAME_INTERVAL_MS, self._send_frame)

    def _show_remote_frame(self, name, b64_frame):
        try:
            buf   = base64.b64decode(b64_frame)
            arr   = np.frombuffer(buf, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except Exception as e:
            print(f"[Video] Erreur : {e}"); return
        if name not in self.tiles:
            self._add_tile(name)
        tile = self.tiles.get(name)
        if tile and tile.winfo_exists():
            tile.update_frame(img)

    # ──────────────────────────────────────────────────────────────────────────
    # Membres + Chat
    # ──────────────────────────────────────────────────────────────────────────
    def _update_members(self, members):
        self.members_lbl.configure(text=f"👥 {len(members)} participant(s)")
        for m in members:
            if m != self.my_name and m not in self.tiles:
                self._add_tile(m)

    def _add_chat_line(self, text):
        self.chat_box.configure(state="normal")
        self.chat_box.insert("end", text + "\n")
        self.chat_box.see("end")
        self.chat_box.configure(state="disabled")

    def _send_chat(self):
        text = self.chat_entry.get().strip()
        if not text or not self.ws:
            return
        if self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.ws.send(json.dumps({"type": "chat", "text": text})),
                self.loop)
        self.chat_entry.delete(0, "end")

    def on_closing(self):
        self._leave()
        self.destroy()


if __name__ == "__main__":
    app = VideoCallApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()











"""
server.py  –  Serveur de salle vidéo partagée
Lancer UNE SEULE FOIS sur le PC "hôte" :
    python server.py
"""
import asyncio
import websockets
import json
import time
from collections import defaultdict

HOST = "0.0.0.0"
PORT = 9765

clients = {}
rooms   = defaultdict(set)


def is_open(ws):
    try:
        return ws.open
    except AttributeError:
        return ws.state.name == "OPEN"


async def broadcast(room, message, exclude=None):
    targets = [ws for ws in rooms[room] if ws != exclude and is_open(ws)]
    if targets:
        await asyncio.gather(*[ws.send(message) for ws in targets],
                             return_exceptions=True)


async def broadcast_members(room):
    members = [clients[ws]["name"] for ws in rooms[room] if ws in clients]
    msg = json.dumps({"type": "members", "members": members})
    targets = [ws for ws in rooms[room] if is_open(ws)]
    if targets:
        await asyncio.gather(*[ws.send(msg) for ws in targets],
                             return_exceptions=True)


async def handler(ws):
    print(f"[+] Connexion : {ws.remote_address}")
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except Exception:
                continue

            t = data.get("type")

            if t == "join":
                name = data.get("name", "Anonyme")[:30]
                # FORCE tout le monde dans la même salle, peu importe ce qu'ils tapent
                room = "Salles_Unique_Pour_Tous" 
                
                clients[ws] = {"name": name, "room": room}
                rooms[room].add(ws)
                print(f"  [GLOBAL] {name} rejoint (Total: {len(rooms[room])} membres)")
                
                await ws.send(json.dumps({
                    "type": "joined", "name": name,
                    "room": "Polymeet", "count": len(rooms[room])
                }))
                await broadcast(room, json.dumps(
                    {"type": "user_joined", "name": name}), exclude=ws)
                await broadcast_members(room)


            elif t == "video":
                if ws in clients:
                    room = clients[ws]["room"]
                    name = clients[ws]["name"]
                    await broadcast(room, json.dumps({
                        "type": "video", "name": name,
                        "frame": data.get("frame", "")
                    }), exclude=ws)

            elif t == "audio":
                if ws in clients:
                    room = clients[ws]["room"]
                    name = clients[ws]["name"]
                    await broadcast(room, json.dumps({
                        "type": "audio", "name": name,
                        "chunk": data.get("chunk", "")
                    }), exclude=ws)

            elif t == "chat":
                if ws in clients:
                    room = clients[ws]["room"]
                    name = clients[ws]["name"]
                    await broadcast(room, json.dumps({
                        "type": "chat", "name": name,
                        "text": data.get("text", "")[:500],
                        "time": time.strftime("%H:%M")
                    }))

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if ws in clients:
            info = clients.pop(ws)
            room = info["room"]
            rooms[room].discard(ws)
            print(f"  [{room}] {info['name']} quitté ({len(rooms[room])} restants)")
            await broadcast(room, json.dumps(
                {"type": "user_left", "name": info["name"]}))
            await broadcast_members(room)
            if not rooms[room]:
                del rooms[room]


async def main():
    import socket
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "127.0.0.1"

    print("╔══════════════════════════════════════════╗")
    print(f"║  Serveur vidéo  –  port {PORT}             ║")
    print("║  En attente de connexions…               ║")
    print("╚══════════════════════════════════════════╝")
    print(f"\n  ✅ IP locale  : {ip}")
    print(f"  📋 Donnez cette IP aux participants : {ip}\n")

    async with websockets.serve(handler, HOST, PORT, max_size=10_000_000):
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main())













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





















"""
install_languages.py — Pré-installation des packs de langue argos-translate
Lancer UNE SEULE FOIS avec internet :
    python install_languages.py

Après ça : traduction 100% offline ✅
"""
import argostranslate.package
import argostranslate.translate

# Langues à installer (paires depuis/vers anglais comme pivot)
# Format : (from_code, to_code)
PAIRS_TO_INSTALL = [
    ("en", "fr"),   # Anglais → Français
    ("fr", "en"),   # Français → Anglais
    ("en", "ar"),   # Anglais → Arabe
    ("en", "es"),   # Anglais → Espagnol
    ("en", "de"),   # Anglais → Allemand
    ("en", "zh"),   # Anglais → Chinois
    ("en", "pt"),   # Anglais → Portugais
    ("en", "it"),   # Anglais → Italien
    ("ar", "en"),
    ("es", "en"),
    ("de", "en"),
    ("zh", "en"),
    ("pt", "en"),
    ("it", "en"),
]

def install_all():
    print("🔄 Mise à jour de l'index des paquets…")
    argostranslate.package.update_package_index()
    available = argostranslate.package.get_available_packages()
    installed = argostranslate.translate.get_installed_languages()
    installed_codes = {l.code for l in installed}

    for from_code, to_code in PAIRS_TO_INSTALL:
        # Vérifier si déjà installé
        from_lang = next((l for l in installed if l.code == from_code), None)
        to_lang   = next((l for l in installed if l.code == to_code), None)
        if from_lang and to_lang and from_lang.get_translation(to_lang):
            print(f"  ✅ {from_code} → {to_code} déjà installé")
            continue

        # Trouver le paquet
        pkg = next((p for p in available
                    if p.from_code == from_code and p.to_code == to_code), None)
        if pkg:
            print(f"  📦 Installation {from_code} → {to_code}…")
            try:
                argostranslate.package.install_from_path(pkg.download())
                print(f"  ✅ {from_code} → {to_code} installé !")
            except Exception as e:
                print(f"  ❌ {from_code} → {to_code} échoué : {e}")
        else:
            print(f"  ⚠  {from_code} → {to_code} non disponible")

    print("\n✅ Installation terminée !")
    print("Vous pouvez maintenant utiliser la traduction offline.\n")

    # Résumé
    installed = argostranslate.translate.get_installed_languages()
    print("Langues disponibles :")
    for l in installed:
        targets = [t.to_lang.code for t in l.translations_from]
        if targets:
            print(f"  {l.code} → {', '.join(targets)}")

if __name__ == "__main__":
    install_all()

















"""
analysis_engine.py  –  Module F-07/F-08 : Analyse IA & Résumé Intelligent
──────────────────────────────────────────────────────────────────────────
100% offline — aucune API externe requise.
Utilise des règles linguistiques, regex et analyse de fréquence.

F-07 Analyse Sémantique :
  • Identification des sujets principaux (TF-IDF simplifié)
  • Détection des décisions prises
  • Extraction des tâches/actions avec responsable et délai
  • Identification des questions sans réponse
  • Analyse du sentiment (positif / neutre / tendu)

F-08 Résumé Intelligent :
  • Résumé narratif (3-5 paragraphes)
  • Résumé exécutif ultra-court (5 lignes max)
  • Points clés hiérarchisés
"""

import re
import math
import datetime
from collections import Counter
from typing import Optional

# ─── Marqueurs de décisions ──────────────────────────────────────────────────
DECISION_MARKERS = [
    r"on a décidé", r"nous avons décidé", r"il a été décidé",
    r"on va", r"nous allons", r"il faut", r"on doit", r"nous devons",
    r"c'est décidé", r"convenu que", r"accord sur", r"validé",
    r"approuvé", r"retenu", r"choisi de", r"opté pour",
    r"we decided", r"it was decided", r"we will", r"we agreed",
    r"decision to", r"agreed to", r"approved", r"resolved to",
    r"nifanaraka", r"voafaritra", r"hataontsika",
]

# ─── Marqueurs de tâches ─────────────────────────────────────────────────────
TASK_MARKERS = [
    r"il faut", r"on doit", r"nous devons", r"à faire",
    r"préparer", r"envoyer", r"créer", r"rédiger", r"contacter",
    r"vérifier", r"analyser", r"organiser", r"planifier",
    r"développer", r"finir", r"terminer", r"livrer",
    r"responsable", r"assigné à", r"délai", r"avant le",
    r"need to", r"must", r"action item", r"todo", r"assigned to",
    r"deadline", r"by monday", r"by friday",
]

# ─── Marqueurs de délais ─────────────────────────────────────────────────────
DEADLINE_PATTERNS = [
    r"avant (?:le )?(\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?)",
    r"pour (?:le )?(\d{1,2}[\/\-]\d{1,2}(?:[\/\-]\d{2,4})?)",
    r"d'ici (?:le )?(.{3,20})",
    r"(?:lundi|mardi|mercredi|jeudi|vendredi)",
    r"(?:monday|tuesday|wednesday|thursday|friday)",
    r"(?:cette|la) semaine",
    r"(?:next week|this week)",
]

# ─── Mots de sentiment ───────────────────────────────────────────────────────
POSITIVE_WORDS = {
    "excellent", "parfait", "bien", "bon", "super", "génial", "bravo",
    "accord", "validé", "approuvé", "succès", "réussi", "avancé",
    "progrès", "positif", "satisfait", "content", "heureux",
    "great", "good", "perfect", "agreed", "success", "happy",
    "satisfied", "positive", "approved", "done", "completed",
}

NEGATIVE_WORDS = {
    "problème", "difficile", "impossible", "retard", "bloqué", "échec",
    "mauvais", "insuffisant", "refusé", "rejeté", "annulé", "urgent",
    "critique", "risque", "préoccupant", "conflit",
    "problem", "difficult", "impossible", "delay", "blocked", "failure",
    "bad", "refused", "rejected", "cancelled", "urgent", "critical",
}

# ─── Stopwords ───────────────────────────────────────────────────────────────
STOPWORDS = {
    "le", "la", "les", "un", "une", "des", "de", "du", "et", "ou",
    "mais", "donc", "car", "si", "que", "qui", "quoi", "comment",
    "je", "tu", "il", "elle", "nous", "vous", "ils", "elles", "on",
    "me", "te", "se", "lui", "leur", "y", "en", "à", "au", "aux",
    "par", "pour", "avec", "sans", "sur", "sous", "dans", "entre",
    "est", "sont", "être", "avoir", "fait", "faire", "dit", "dire",
    "the", "a", "an", "is", "are", "was", "were", "be", "have", "has",
    "do", "does", "will", "would", "could", "should", "can", "of",
    "in", "to", "for", "on", "at", "by", "from", "with", "and", "or",
    "but", "if", "not", "this", "that", "it", "we", "they", "you",
    "notre", "votre", "mon", "ton", "son", "ma", "ta", "sa", "ce",
    "cette", "ces", "dont", "plus", "très", "bien", "aussi", "alors",
    "même", "tout", "tous", "c'est", "cet", "où",
}


class MeetingAnalysis:
    """Résultat complet d'une analyse de réunion."""

    def __init__(self):
        self.topics: list           = []
        self.decisions: list        = []
        self.tasks: list            = []
        self.questions: list        = []
        self.sentiment: str         = "neutre"
        self.sentiment_score: float = 0.0
        self.summary_short: str     = ""
        self.summary_full: str      = ""
        self.key_points: list       = []
        self.word_count: int        = 0
        self.speakers_mentioned: list = []
        self.analyzed_at: str       = datetime.datetime.now().strftime("%d/%m/%Y %H:%M")

    def to_dict(self) -> dict:
        return {
            "topics":             self.topics,
            "decisions":          self.decisions,
            "tasks":              self.tasks,
            "questions":          self.questions,
            "sentiment":          self.sentiment,
            "sentiment_score":    self.sentiment_score,
            "summary_short":      self.summary_short,
            "summary_full":       self.summary_full,
            "key_points":         self.key_points,
            "word_count":         self.word_count,
            "speakers_mentioned": self.speakers_mentioned,
            "analyzed_at":        self.analyzed_at,
        }


class AnalysisEngine:
    """
    Moteur d'analyse sémantique 100% offline.

    Usage :
        engine   = AnalysisEngine()
        analysis = engine.analyze(transcript_text, speakers=["Alice", "Bob"])
        print(analysis.summary_short)
        print(analysis.decisions)
        report = engine.format_analysis_report(analysis)
    """

    def analyze(self, transcript: str,
                speakers: Optional[list] = None) -> MeetingAnalysis:
        result = MeetingAnalysis()

        if not transcript or not transcript.strip():
            result.summary_short = "Aucune transcription disponible."
            return result

        text      = self._clean_text(transcript)
        sentences = self._split_sentences(text)
        words     = self._tokenize(text)

        result.word_count         = len(words)
        result.topics             = self._extract_topics(words, sentences)
        result.decisions          = self._extract_decisions(sentences)
        result.tasks              = self._extract_tasks(sentences, speakers or [])
        result.questions          = self._extract_unanswered_questions(sentences)
        result.sentiment, result.sentiment_score = self._analyze_sentiment(words)
        result.speakers_mentioned = self._extract_speakers(text, speakers or [])
        result.key_points         = self._extract_key_points(result)
        result.summary_short      = self._generate_short_summary(result)
        result.summary_full       = self._generate_full_summary(result, sentences)

        return result

    # ── Sujets ───────────────────────────────────────────────────────────────

    def _extract_topics(self, words: list, sentences: list,
                        max_topics: int = 6) -> list:
        meaningful = [w.lower() for w in words
                      if len(w) > 3 and w.lower() not in STOPWORDS and w.isalpha()]
        if not meaningful:
            return []

        freq  = Counter(meaningful)
        total = len(meaningful)
        scores = {w: (c / total) * (math.log(total / (c + 1)) + 1)
                  for w, c in freq.items()}

        topics = []
        seen   = set()
        for word, _ in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            if len(topics) >= max_topics:
                break
            if word not in seen:
                ctx = next((s for s in sentences if word in s.lower()), "")
                phrase = self._noun_phrase(word, ctx)
                if phrase and phrase not in seen:
                    topics.append(phrase)
                    seen.add(phrase)
                    seen.add(word)
        return topics

    def _noun_phrase(self, word: str, sentence: str, window: int = 3) -> str:
        words = sentence.split()
        try:
            idx   = next(i for i, w in enumerate(words) if word in w.lower())
            start = max(0, idx - 1)
            end   = min(len(words), idx + window)
            phrase = " ".join(words[start:end])
            phrase = re.sub(r'[^\w\s\-]', '', phrase).strip()
            return phrase[:50] if phrase else word
        except StopIteration:
            return word

    # ── Décisions ────────────────────────────────────────────────────────────

    def _extract_decisions(self, sentences: list) -> list:
        decisions = []
        for s in sentences:
            for marker in DECISION_MARKERS:
                if re.search(marker, s.lower()):
                    cleaned = s.strip()
                    if len(cleaned) > 10 and cleaned not in decisions:
                        decisions.append(cleaned)
                    break
        return decisions[:10]

    # ── Tâches ───────────────────────────────────────────────────────────────

    def _extract_tasks(self, sentences: list, speakers: list) -> list:
        tasks = []
        for s in sentences:
            if any(re.search(m, s.lower()) for m in TASK_MARKERS):
                if len(s.strip()) > 10:
                    task = {
                        "text":     s.strip(),
                        "person":   self._find_person(s, speakers),
                        "deadline": self._find_deadline(s),
                        "status":   "À faire",
                    }
                    if not any(t["text"] == task["text"] for t in tasks):
                        tasks.append(task)
        return tasks[:15]

    def _find_person(self, sentence: str, speakers: list) -> str:
        for sp in speakers:
            if sp and sp.lower() in sentence.lower():
                return sp
        matches = re.findall(r'\b([A-Z][a-zéèêàâîïôùûç]+)\b', sentence)
        filtered = [m for m in matches if m.lower() not in STOPWORDS and len(m) > 2]
        return filtered[0] if filtered else "—"

    def _find_deadline(self, sentence: str) -> str:
        for pattern in DEADLINE_PATTERNS:
            match = re.search(pattern, sentence, re.IGNORECASE)
            if match:
                return match.group(0).strip()
        return "—"

    # ── Questions sans réponse ────────────────────────────────────────────────

    def _extract_unanswered_questions(self, sentences: list) -> list:
        response_starters = {
            "oui", "non", "bien sûr", "effectivement", "absolument",
            "yes", "no", "of course", "exactly", "je pense",
        }
        questions = []
        for i, s in enumerate(sentences):
            if s.strip().endswith("?") and len(s) > 10:
                answered = False
                if i + 1 < len(sentences):
                    next_s = sentences[i + 1].lower().strip()
                    answered = any(next_s.startswith(r) for r in response_starters)
                if not answered:
                    questions.append(s.strip())
        return questions[:8]

    # ── Sentiment ─────────────────────────────────────────────────────────────

    def _analyze_sentiment(self, words: list) -> tuple:
        if not words:
            return "neutre", 0.0
        w_lower   = [w.lower() for w in words]
        pos_count = sum(1 for w in w_lower if w in POSITIVE_WORDS)
        neg_count = sum(1 for w in w_lower if w in NEGATIVE_WORDS)
        score     = (pos_count - neg_count) / max(len(w_lower) * 0.1, 1)
        score     = max(-1.0, min(1.0, score))
        if score > 0.2:
            label = "positif"
        elif score < -0.2:
            label = "tendu"
        else:
            label = "neutre"
        return label, round(score, 2)

    # ── Speakers ──────────────────────────────────────────────────────────────

    def _extract_speakers(self, text: str, speakers: list) -> list:
        return [sp for sp in speakers if sp and sp.lower() in text.lower()]

    # ── Points clés ──────────────────────────────────────────────────────────

    def _extract_key_points(self, a: MeetingAnalysis) -> list:
        points = []
        if a.topics:
            points.append(f"📌 Sujets : {', '.join(a.topics[:4])}")
        for d in a.decisions[:3]:
            points.append(f"✅ Décision : {d[:100]}")
        for t in a.tasks[:4]:
            p = f" ({t['person']})" if t['person'] != "—" else ""
            dl = f" — délai : {t['deadline']}" if t['deadline'] != "—" else ""
            points.append(f"📋 Action{p} : {t['text'][:80]}{dl}")
        if a.questions:
            points.append(f"❓ {len(a.questions)} question(s) sans réponse")
        emoji = {"positif": "😊", "neutre": "😐", "tendu": "😟"}
        points.append(
            f"{emoji.get(a.sentiment,'😐')} Ambiance : {a.sentiment}")
        return points

    # ── Résumé court ──────────────────────────────────────────────────────────

    def _generate_short_summary(self, a: MeetingAnalysis) -> str:
        participants = (", ".join(a.speakers_mentioned)
                        if a.speakers_mentioned else "les participants")
        lines = [
            f"Réunion du {a.analyzed_at} avec {participants}.",
            f"Sujets : {', '.join(a.topics[:3])}." if a.topics
            else "Aucun sujet principal identifié.",
            (f"{len(a.decisions)} décision(s) prise(s)."
             if a.decisions else "Aucune décision formelle."),
            (f"{len(a.tasks)} action(s) identifiée(s)."
             if a.tasks else "Aucune tâche identifiée."),
            f"Ambiance générale : {a.sentiment}.",
        ]
        return "\n".join(lines)

    # ── Résumé narratif complet ───────────────────────────────────────────────

    def _generate_full_summary(self, a: MeetingAnalysis,
                               sentences: list) -> str:
        participants = (", ".join(a.speakers_mentioned)
                        if a.speakers_mentioned else "les participants")
        topics_str   = ", ".join(a.topics[:4]) if a.topics else "divers sujets"

        p1 = (f"Cette réunion, analysée le {a.analyzed_at}, "
              f"a réuni {participants}. "
              f"Les échanges ont principalement porté sur : {topics_str}. "
              f"La transcription contient {a.word_count} mots au total.")

        paragraphs = [p1]

        if a.decisions:
            dec_str = " ; ".join(
                f"({i+1}) {d[:100]}" for i, d in enumerate(a.decisions[:3]))
            paragraphs.append(
                f"Au cours de cette réunion, {len(a.decisions)} décision(s) "
                f"ont été identifiées : {dec_str}.")

        if a.tasks:
            task_parts = []
            for t in a.tasks[:4]:
                part = t['text'][:80]
                if t['person'] != "—":
                    part += f" (responsable : {t['person']})"
                if t['deadline'] != "—":
                    part += f" — avant {t['deadline']}"
                task_parts.append(part)
            paragraphs.append(
                f"{len(a.tasks)} action(s) ont été identifiées : "
                + " ; ".join(task_parts) + ".")

        if a.questions:
            q_str = " / ".join(q[:60] for q in a.questions[:3])
            paragraphs.append(
                f"Plusieurs questions sont restées sans réponse : {q_str}. "
                f"Ces points méritent un suivi lors de la prochaine réunion.")

        desc = {"positif": "constructive et productive",
                "neutre": "neutre et factuelle",
                "tendu": "parfois tendue avec des points de friction"}
        paragraphs.append(
            f"Dans l'ensemble, l'ambiance a été "
            f"{desc.get(a.sentiment, 'neutre')} "
            f"(score : {a.sentiment_score:+.2f}). "
            f"Il est recommandé de faire un suivi des actions identifiées.")

        return "\n\n".join(paragraphs)

    # ── Utilitaires ───────────────────────────────────────────────────────────

    def _clean_text(self, text: str) -> str:
        text = re.sub(r'\[\d{2}:\d{2}:\d{2}\]', '', text)
        text = re.sub(r'\([A-Z]{2}\)', '', text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def _split_sentences(self, text: str) -> list:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        return [s.strip() for s in sentences if len(s.strip()) > 5]

    def _tokenize(self, text: str) -> list:
        return re.findall(r'\b[a-zA-ZÀ-ÿ]{2,}\b', text)

    def format_analysis_report(self, analysis: MeetingAnalysis) -> str:
        """Formate l'analyse complète en texte lisible pour l'export."""
        lines = [
            "═" * 55,
            "  ANALYSE DE LA RÉUNION — PolyMeet",
            f"  Analysé le : {analysis.analyzed_at}",
            "═" * 55,
            "",
            "📌 SUJETS PRINCIPAUX",
        ]
        for i, t in enumerate(analysis.topics, 1):
            lines.append(f"  {i}. {t}")

        lines += ["", "✅ DÉCISIONS PRISES"]
        if analysis.decisions:
            for d in analysis.decisions:
                lines.append(f"  • {d[:120]}")
        else:
            lines.append("  Aucune décision formelle détectée.")

        lines += ["", "📋 TÂCHES ET ACTIONS"]
        if analysis.tasks:
            for t in analysis.tasks:
                lines.append(f"  • {t['text'][:100]}")
                if t['person'] != "—":
                    lines.append(f"    → Responsable : {t['person']}")
                if t['deadline'] != "—":
                    lines.append(f"    → Délai : {t['deadline']}")
        else:
            lines.append("  Aucune tâche détectée.")

        lines += ["", "❓ QUESTIONS SANS RÉPONSE"]
        if analysis.questions:
            for q in analysis.questions:
                lines.append(f"  • {q[:100]}")
        else:
            lines.append("  Toutes les questions ont reçu une réponse.")

        emoji = {"positif": "😊", "neutre": "😐", "tendu": "😟"}
        lines += [
            "",
            f"{emoji.get(analysis.sentiment,'😐')} SENTIMENT : "
            f"{analysis.sentiment.upper()} "
            f"(score : {analysis.sentiment_score:+.2f})",
            "",
            "─" * 55,
            "RÉSUMÉ EXÉCUTIF (5 lignes)",
            "─" * 55,
            analysis.summary_short,
            "",
            "─" * 55,
            "RÉSUMÉ COMPLET",
            "─" * 55,
            analysis.summary_full,
            "",
            "═" * 55,
        ]
        return "\n".join(lines)