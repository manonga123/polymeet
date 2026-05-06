"""
client.py  –  Client de salle vidéo + audio partagée
─────────────────────────────────────────────────────
  [F-02] Contrôle de l'Enregistrement
  [F-03] Transcription Automatique
  [F-04] Diarisation / Identification Speakers
  [F-05/F-06] Traduction Multilingue (onglet dédié)
  [F-07/F-08] Analyse IA
  [F-09/F-10/F-11] Génération Rapports Word/PDF
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

    import shutil, sys
    _ffmpeg = shutil.which("ffmpeg")
    if not _ffmpeg:
        _venv_dirs = []
        if hasattr(sys, "prefix"):
            _venv_dirs += [
                os.path.join(sys.prefix, "bin", "ffmpeg.exe"),
                os.path.join(sys.prefix, "Scripts", "ffmpeg.exe"),
                os.path.join(sys.prefix, "Library", "bin", "ffmpeg.exe"),
            ]
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

# ── Imports moteurs ──────────────────────────────────────────────────────────
try:
    from transcription_engine import TranscriptionEngine
    TRANSCRIPTION_AVAILABLE = True
except ImportError:
    TRANSCRIPTION_AVAILABLE = False
    print("[Client] transcription_engine.py introuvable — F-03 désactivé")

try:
    from diarization_engine import DiarizationEngine, SPEAKER_COLORS
    DIARIZATION_AVAILABLE = True
except ImportError:
    DIARIZATION_AVAILABLE = False
    print("[Client] diarization_engine.py introuvable — F-04 désactivé")

try:
    from translation_engine import TranslationEngine, SUPPORTED_LANGUAGES, LANG_NAMES
    TRANSLATION_AVAILABLE = True
except ImportError:
    TRANSLATION_AVAILABLE = False
    print("[Client] translation_engine.py introuvable — F-05 désactivé")

try:
    from analysis_engine import AnalysisEngine, MeetingAnalysis
    ANALYSIS_AVAILABLE = True
except ImportError:
    ANALYSIS_AVAILABLE = False
    print("[Client] analysis_engine.py introuvable — F-07 désactivé")

try:
    from report_engine import ReportEngine, ReportConfig
    REPORT_AVAILABLE = True
except ImportError:
    REPORT_AVAILABLE = False
    print("[Client] report_engine.py introuvable — F-09 désactivé")

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ─── Paramètres réseau ──────────────────────────────────────────────────────
DROIDCAM_IP   = "192.168.O.14"
DROIDCAM_PORT = 4747
SERVER_IP     = "192.168.56.1"
SERVER_PORT   = 9765

# ─── Paramètres vidéo ───────────────────────────────────────────────────────
FRAME_QUALITY     = 50
FRAME_RESIZE      = (320, 240)
FRAME_INTERVAL_MS = 50        # ~20 fps

# ─── Paramètres audio ───────────────────────────────────────────────────────
AUDIO_RATE      = 44100
AUDIO_CHANNELS  = 1
AUDIO_FORMAT    = pyaudio.paInt16
AUDIO_CHUNK     = 2048
AUDIO_VAD_RMS   = 300
AUDIO_QUEUE_MAX = 8
WHISPER_RATE    = 16000

# ─── Enregistrement ─────────────────────────────────────────────────────────
RECORDINGS_DIR      = "recordings"
AUTOSAVE_INTERVAL_S = 300
os.makedirs(RECORDINGS_DIR, exist_ok=True)


# ─── Liste des microphones disponibles ──────────────────────────────────────
def list_microphones():
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
            bg=bar_bg, fg="white", font=("Arial", 10, "bold"), anchor="w")
        self.name_lbl.pack(side="left", fill="both", expand=True)
        self.mic_indicator = tk.Label(
            self.name_bar, text="🎤", bg=bar_bg, font=("Arial", 9), fg="#555")
        self.mic_indicator.pack(side="right", padx=4)
        self.img_label = tk.Label(
            self, text="⏳\nEn attente…", bg=bg, fg="#555", font=("Arial", 16))
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
        self.device_index    = device_index
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
        if self.device_index is None:
            try:
                info = self.pa.get_default_input_device_info()
                self.device_index = info["index"]
                self.device_rate  = int(info["defaultSampleRate"])
            except Exception:
                pass
        try:
            kwargs = dict(
                format=AUDIO_FORMAT, channels=AUDIO_CHANNELS,
                rate=self.device_rate, input=True,
                frames_per_buffer=AUDIO_CHUNK,
                stream_callback=self._capture_callback)
            if self.device_index is not None:
                kwargs["input_device_index"] = self.device_index
            self.in_stream = self.pa.open(**kwargs)
            self.in_stream.start_stream()
        except Exception as e:
            print(f"[Audio] ❌ Micro : {e}")
            return False
        try:
            self.out_stream = self.pa.open(
                format=AUDIO_FORMAT, channels=AUDIO_CHANNELS,
                rate=self.device_rate, output=True,
                frames_per_buffer=AUDIO_CHUNK)
        except Exception as e:
            print(f"[Audio] ❌ HP : {e}")
            return False
        self.running    = True
        self._pb_thread = threading.Thread(target=self._playback_loop, daemon=True)
        self._pb_thread.start()
        return True

    def stop(self):
        self.running = False
        for s in (self.in_stream, self.out_stream):
            if s:
                try: s.stop_stream(); s.close()
                except: pass
        self.in_stream = self.out_stream = None
        if self.pa:
            try: self.pa.terminate()
            except: pass
            self.pa = None
        with self._lock:
            self.audio_queues.clear()

    def toggle_mute(self):
        self.muted = not self.muted
        return self.muted

    def receive_chunk(self, name, b64_chunk):
        try:
            raw = base64.b64decode(b64_chunk)
        except: return
        with self._lock:
            if name not in self.audio_queues:
                self.audio_queues[name] = deque(maxlen=AUDIO_QUEUE_MAX)
            self.audio_queues[name].append(raw)

    def remove_participant(self, name):
        with self._lock:
            self.audio_queues.pop(name, None)

    def resample_to_whisper(self, raw_bytes):
        if self.device_rate == WHISPER_RATE:
            return raw_bytes
        try:
            samples   = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
            ratio     = WHISPER_RATE / self.device_rate
            new_len   = max(1, int(len(samples) * ratio))
            resampled = np.interp(
                np.linspace(0, len(samples)-1, new_len),
                np.arange(len(samples)), samples).astype(np.int16)
            return resampled.tobytes()
        except: return raw_bytes

    def _capture_callback(self, in_data, frame_count, time_info, status):
        if in_data:
            if self.on_record_chunk:
                self.on_record_chunk(in_data)
            if not self.muted:
                samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
                rms = float(np.sqrt(np.mean(samples**2))) if len(samples) > 0 else 0.0
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
                    q     = self.audio_queues.get(name)
                    chunk = q.popleft() if q and len(q) > 0 else None
                if chunk and len(chunk) == AUDIO_CHUNK * 2:
                    arr   = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    mixed = arr if mixed is None else mixed + arr
            if mixed is not None:
                mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
                data  = mixed.tobytes()
            else:
                data = silence
            if self.out_stream and self.running:
                try: self.out_stream.write(data)
                except: pass


# ═══════════════════════════════════════════════════════════════════════════════
# [F-02] RecordingEngine
# ═══════════════════════════════════════════════════════════════════════════════
class RecordingEngine:
    STATE_IDLE      = "idle"
    STATE_RECORDING = "recording"
    STATE_PAUSED    = "paused"

    def __init__(self):
        self.state           = self.STATE_IDLE
        self._frames         = []
        self._all_frames     = []
        self._lock           = threading.Lock()
        self._start_time     = None
        self._pause_time     = None
        self._total_paused   = 0.0
        self._session_name   = ""
        self._log_entries    = []
        self._autosave_timer = None

    @property
    def is_recording(self):
        return self.state == self.STATE_RECORDING

    @property
    def elapsed_seconds(self):
        if self._start_time is None:
            return 0.0
        total  = (datetime.datetime.now() - self._start_time).total_seconds()
        paused = self._total_paused
        if self.state == self.STATE_PAUSED and self._pause_time:
            paused += (datetime.datetime.now() - self._pause_time).total_seconds()
        return max(0.0, total - paused)

    def start(self, session_name=""):
        if self.state != self.STATE_IDLE:
            return False
        self._start_time   = datetime.datetime.now()
        self._total_paused = 0.0
        self._pause_time   = None
        self._frames       = []
        self._all_frames   = []
        self._log_entries  = []
        self._session_name = session_name or self._start_time.strftime("reunion_%Y%m%d_%H%M%S")
        self.state         = self.STATE_RECORDING
        self._schedule_autosave()
        self._log("SYSTEM", "Enregistrement démarré")
        return True

    def pause(self):
        if self.state != self.STATE_RECORDING:
            return False
        self._pause_time = datetime.datetime.now()
        self.state       = self.STATE_PAUSED
        self._cancel_autosave()
        self._log("SYSTEM", "Pause")
        return True

    def resume(self):
        if self.state != self.STATE_PAUSED:
            return False
        if self._pause_time:
            self._total_paused += (datetime.datetime.now() - self._pause_time).total_seconds()
        self._pause_time = None
        self.state       = self.STATE_RECORDING
        self._schedule_autosave()
        self._log("SYSTEM", "Reprise")
        return True

    def stop(self):
        if self.state == self.STATE_IDLE:
            return None
        self._cancel_autosave()
        if self.state == self.STATE_PAUSED and self._pause_time:
            self._total_paused += (datetime.datetime.now() - self._pause_time).total_seconds()
        self._log("SYSTEM", f"Arrêté — {self._fmt_duration(self.elapsed_seconds)}")
        self.state = self.STATE_IDLE
        path = self._save(self._all_frames, self._session_name, final=True)
        self._save_log()
        return path

    def add_chunk(self, raw_bytes):
        if self.state != self.STATE_RECORDING:
            return
        with self._lock:
            self._frames.append(raw_bytes)
            self._all_frames.append(raw_bytes)

    def log_intervention(self, speaker, note=""):
        if self.state == self.STATE_IDLE:
            return
        self._log(speaker, note or "Intervention")

    def _log(self, speaker, note):
        ts    = datetime.datetime.now().strftime("%H:%M:%S")
        dur   = self._fmt_duration(self.elapsed_seconds)
        entry = f"[{ts}] (+{dur})  {speaker}: {note}"
        self._log_entries.append(entry)

    def _schedule_autosave(self):
        self._autosave_timer = threading.Timer(AUTOSAVE_INTERVAL_S, self._autosave_callback)
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
            frames_copy  = list(self._frames)
            self._frames = []
        ts   = datetime.datetime.now().strftime("%H%M%S")
        name = f"{self._session_name}_autosave_{ts}"
        self._save(frames_copy, name, final=False)
        self._schedule_autosave()

    def _save(self, frames, name, final):
        if not frames:
            return ""
        wav_path = os.path.join(RECORDINGS_DIR, f"{name}.wav")
        try:
            with wave.open(wav_path, "wb") as wf:
                wf.setnchannels(AUDIO_CHANNELS)
                wf.setsampwidth(2)
                wf.setframerate(AUDIO_RATE)
                wf.writeframes(b"".join(frames))
        except Exception as e:
            print(f"[Record] ❌ WAV : {e}")
            return ""
        if final and PYDUB_AVAILABLE:
            mp3_path = os.path.join(RECORDINGS_DIR, f"{name}.mp3")
            try:
                AudioSegment.from_wav(wav_path).export(mp3_path, format="mp3", bitrate="64k")
                os.remove(wav_path)
                return mp3_path
            except: return wav_path
        return wav_path

    def _save_log(self):
        if not self._log_entries:
            return
        path = os.path.join(RECORDINGS_DIR, f"{self._session_name}_log.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("PolyMeet — Journal\n")
                f.write(f"Session : {self._session_name}\n")
                f.write("─" * 50 + "\n\n")
                for e in self._log_entries:
                    f.write(e + "\n")
        except Exception as e:
            print(f"[Record] ❌ Log : {e}")

    @staticmethod
    def _fmt_duration(seconds):
        s = int(seconds)
        h, r = divmod(s, 3600)
        m, s = divmod(r, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


# ═════════════════════════════════════════════════════════════════════════════
# Application principale
# ═════════════════════════════════════════════════════════════════════════════
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
        self.cam_sources = []
        self.cam_index   = 0
        self.cam_source  = None
        self.mic_sources = []
        self.mic_index   = 0

        self.audio_engine         = None
        self.recording_engine     = RecordingEngine()
        self.transcription_engine = None
        self.diarization_engine   = None
        self.translation_engine   = None
        self.analysis_engine      = AnalysisEngine() if ANALYSIS_AVAILABLE else None
        self.report_engine        = ReportEngine()   if REPORT_AVAILABLE   else None
        self._last_analysis       = None
        self._transcript_entries  = []

        self.tiles   = {}
        self.my_tile = None
        self._clock_job = None

        self._build_ui()
        self.bind("<Configure>", lambda e: self.after(100, self._relayout_grid))
        self.bind("<Control-r>", lambda e: self._rec_start())
        self.bind("<Control-p>", lambda e: self._rec_pause_resume())
        self.bind("<Control-s>", lambda e: self._rec_stop())

    # ── UI ───────────────────────────────────────────────────────────────────
    def _build_ui(self):
        topbar = ctk.CTkFrame(self, height=52, fg_color="#161b22", corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)
        ctk.CTkLabel(topbar, text="🎥 PolyMeet", font=("Arial", 18, "bold")).pack(side="left", padx=18)
        self.status_lbl = ctk.CTkLabel(topbar, text="⬤ Déconnecté", font=("Arial", 12), text_color="#888")
        self.status_lbl.pack(side="right", padx=18)
        self.members_lbl = ctk.CTkLabel(topbar, text="👥 0 participant(s)", font=("Arial", 11), text_color="#aaa")
        self.members_lbl.pack(side="right", padx=10)

        form = ctk.CTkFrame(self, fg_color="#0d1117", corner_radius=0, height=50)
        form.pack(fill="x", side="top")
        form.pack_propagate(False)
        ctk.CTkLabel(form, text="Nom :", width=40, font=("Arial", 11)).pack(side="left", padx=(14,2))
        self.name_entry = ctk.CTkEntry(form, placeholder_text="Votre prénom", width=120, height=32)
        self.name_entry.pack(side="left", padx=(0,8))
        ctk.CTkLabel(form, text="Salle :", width=40, font=("Arial", 11)).pack(side="left", padx=(0,2))
        self.room_entry = ctk.CTkEntry(form, placeholder_text="general", width=100, height=32)
        self.room_entry.pack(side="left", padx=(0,8))
        ctk.CTkLabel(form, text="IP :", width=25, font=("Arial", 11)).pack(side="left", padx=(0,2))
        self.server_entry = ctk.CTkEntry(form, placeholder_text=SERVER_IP, width=120, height=32)
        self.server_entry.pack(side="left", padx=(0,8))
        self.cam_status = ctk.CTkLabel(form, text="📷 —", font=("Arial", 10), text_color="#888")
        self.cam_status.pack(side="left", padx=(0,4))
        self.mic_status = ctk.CTkLabel(form, text="🎤 —", font=("Arial", 10), text_color="#888")
        self.mic_status.pack(side="left", padx=(0,4))
        self._mic_var = tk.StringVar(value="— Micro —")
        self.mic_selector = ctk.CTkOptionMenu(form, variable=self._mic_var, values=["— Micro —"],
                                              width=170, height=28, font=("Arial", 9),
                                              fg_color="#1a2a3a", button_color="#1e3248",
                                              command=self._on_mic_selected)
        self.mic_selector.pack(side="left", padx=(0,6))
        self._populate_mic_selector()
        self.join_btn = ctk.CTkButton(form, text="📞 Rejoindre", width=110, height=32,
                                       fg_color="#1a7a3c", hover_color="#145e2e",
                                       font=("Arial", 12, "bold"), command=self._join)
        self.join_btn.pack(side="left", padx=(0,6))
        self.leave_btn = ctk.CTkButton(form, text="📵 Quitter", width=90, height=32,
                                        fg_color="#b03030", hover_color="#8a2020",
                                        font=("Arial", 12, "bold"), command=self._leave, state="disabled")
        self.leave_btn.pack(side="left", padx=(0,6))
        self.mute_btn = ctk.CTkButton(form, text="🎤 Mute", width=84, height=32,
                                       fg_color="#444", hover_color="#555",
                                       font=("Arial", 11, "bold"), command=self._toggle_mute, state="disabled")
        self.mute_btn.pack(side="left", padx=(0,6))
        self.cam_btn = ctk.CTkButton(form, text="🔄 Caméra", width=94, height=32,
                                      fg_color="#1a3a6a", hover_color="#142c52",
                                      font=("Arial", 11, "bold"), command=self._switch_camera, state="disabled")
        self.cam_btn.pack(side="left", padx=(0,6))

        center = ctk.CTkFrame(self, fg_color="transparent")
        center.pack(fill="both", expand=True)
        grid_outer = ctk.CTkFrame(center, fg_color="#0d1117", corner_radius=0)
        grid_outer.pack(side="left", fill="both", expand=True)
        self.grid_canvas = tk.Frame(grid_outer, bg="#0d1117")
        self.grid_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        right_panel = ctk.CTkFrame(center, width=290, fg_color="#161b22", corner_radius=0)
        right_panel.pack(side="right", fill="y")
        right_panel.pack_propagate(False)

        tab_bar = ctk.CTkFrame(right_panel, fg_color="#0d1117", corner_radius=0)
        tab_bar.pack(fill="x", side="top")
        tab_row1 = ctk.CTkFrame(tab_bar, fg_color="#0d1117", height=34)
        tab_row1.pack(fill="x")
        tab_row1.pack_propagate(False)
        tab_row2 = ctk.CTkFrame(tab_bar, fg_color="#0d1117", height=34)
        tab_row2.pack(fill="x")
        tab_row2.pack_propagate(False)

        self._active_tab = tk.StringVar(value="chat")
        self.tab_chat_btn = ctk.CTkButton(tab_row1, text="💬 Chat", width=136, height=32,
                                          fg_color="#1a2a3a", hover_color="#1e3248",
                                          font=("Arial", 10, "bold"),
                                          command=lambda: self._switch_tab("chat"))
        self.tab_chat_btn.pack(side="left", padx=(2,1), pady=1)
        self.tab_trans_btn = ctk.CTkButton(tab_row1, text="📝 Transcription", width=136, height=32,
                                           fg_color="#111820", hover_color="#1e3248",
                                           font=("Arial", 10),
                                           command=lambda: self._switch_tab("transcription"))
        self.tab_trans_btn.pack(side="left", padx=(1,2), pady=1)
        self.tab_trad_btn = ctk.CTkButton(tab_row2, text="🌍 Traduction", width=136, height=32,
                                          fg_color="#111820", hover_color="#1e3248",
                                          font=("Arial", 10),
                                          command=lambda: self._switch_tab("traduction"))
        self.tab_trad_btn.pack(side="left", padx=(2,1), pady=1)
        self.tab_report_btn = ctk.CTkButton(tab_row2, text="📊 Rapport", width=136, height=32,
                                            fg_color="#111820", hover_color="#1e3248",
                                            font=("Arial", 10),
                                            command=lambda: self._switch_tab("rapport"))
        self.tab_report_btn.pack(side="left", padx=(1,2), pady=1)

        self._build_tab_chat(right_panel)
        self._build_tab_transcription(right_panel)
        self._build_tab_traduction(right_panel)
        self._build_tab_rapport(right_panel)

        self._switch_tab("chat")
        self._build_recording_panel()

    def _switch_tab(self, tab):
        self._active_tab.set(tab)
        for fr in [self.chat_frame, self.trans_frame, self.trad_frame, self.rapport_frame]:
            if fr.winfo_ismapped():
                fr.pack_forget()
        if tab == "chat":
            self.chat_frame.pack(fill="both", expand=True)
            self.tab_chat_btn.configure(fg_color="#1a2a3a")
        else:
            self.tab_chat_btn.configure(fg_color="#111820")
        if tab == "transcription":
            self.trans_frame.pack(fill="both", expand=True)
            self.tab_trans_btn.configure(fg_color="#1a2a3a")
        else:
            self.tab_trans_btn.configure(fg_color="#111820")
        if tab == "traduction":
            self.trad_frame.pack(fill="both", expand=True)
            self.tab_trad_btn.configure(fg_color="#1a2a3a")
        else:
            self.tab_trad_btn.configure(fg_color="#111820")
        if tab == "rapport":
            self.rapport_frame.pack(fill="both", expand=True)
            self.tab_report_btn.configure(fg_color="#1a2a3a")
        else:
            self.tab_report_btn.configure(fg_color="#111820")

    def _build_tab_chat(self, parent):
        self.chat_frame = ctk.CTkFrame(parent, fg_color="transparent")
        self.chat_box = ctk.CTkTextbox(self.chat_frame, font=("Arial", 11), wrap="word")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(6,4))
        self.chat_box.configure(state="disabled")
        chat_row = ctk.CTkFrame(self.chat_frame, fg_color="transparent")
        chat_row.pack(fill="x", padx=8, pady=(0,10))
        self.chat_entry = ctk.CTkEntry(chat_row, placeholder_text="Message…", height=32)
        self.chat_entry.pack(side="left", fill="x", expand=True, padx=(0,6))
        self.chat_entry.bind("<Return>", lambda e: self._send_chat())
        ctk.CTkButton(chat_row, text="↩", width=34, height=32, command=self._send_chat).pack(side="left")

    # ── Transcription (devenue scrollable pour garantir tous les boutons visibles) ──
    def _build_tab_transcription(self, parent):
        self.trans_frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")  # <-- scrollable !

        # Speakers
        self.speakers_panel = ctk.CTkFrame(
            self.trans_frame, fg_color="#0d1520",
            corner_radius=6, border_width=1, border_color="#1e2d45")
        self.speakers_panel.pack(fill="x", padx=8, pady=(6,4))
        spk_header = ctk.CTkFrame(self.speakers_panel, fg_color="transparent")
        spk_header.pack(fill="x", padx=8, pady=(4,2))
        ctk.CTkLabel(spk_header, text="👥 Participants",
                     font=("Arial", 10, "bold"), text_color="#4a7abf").pack(side="left")
        self.spk_count_lbl = ctk.CTkLabel(spk_header, text="0 speaker(s)", font=("Arial", 9), text_color="#555")
        self.spk_count_lbl.pack(side="right")
        self.spk_list_frame = ctk.CTkScrollableFrame(
            self.speakers_panel, fg_color="transparent", height=60,
            scrollbar_button_color="#1e2d45")
        self.spk_list_frame.pack(fill="x", padx=4, pady=(0,4))
        self._speaker_badges = {}

        trans_top = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trans_top.pack(fill="x", padx=8, pady=(2,2))
        ctk.CTkLabel(trans_top, text="Langue :", font=("Arial", 10), text_color="#555").pack(side="left")
        self.trans_lang_lbl = ctk.CTkLabel(trans_top, text="—", font=("Arial", 10, "bold"), text_color="#4a9abf")
        self.trans_lang_lbl.pack(side="left", padx=(4,0))
        self.trans_status_lbl = ctk.CTkLabel(trans_top, text="⬤ Inactif", font=("Arial", 10), text_color="#444")
        self.trans_status_lbl.pack(side="right")

        self.trans_box = ctk.CTkTextbox(self.trans_frame, font=("Arial", 11), wrap="word", height=140)  # hauteur fixe
        self.trans_box.pack(fill="x", padx=8, pady=(2,4))   # ne plus expand à fond pour laisser les boutons visibles
        self.trans_box.insert("end", "La transcription apparaîtra ici…\n")
        self.trans_box.configure(text_color="#555")

        # Boutons actions
        trans_btns = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trans_btns.pack(fill="x", padx=8, pady=(0,6))
        self.trans_toggle_btn = ctk.CTkButton(
            trans_btns, text="▶ Activer", width=100, height=30,
            fg_color="#1a4a6a", hover_color="#143a54",
            font=("Arial", 10, "bold"), command=self._toggle_transcription)
        self.trans_toggle_btn.pack(side="left", padx=(0,4))
        ctk.CTkButton(trans_btns, text="💾 Exporter", width=80, height=30,
                      fg_color="#2a3a2a", hover_color="#1e2e1e",
                      font=("Arial", 10), command=self._export_transcript).pack(side="left", padx=(0,4))
        ctk.CTkButton(trans_btns, text="🗑", width=30, height=30,
                      fg_color="#3a2020", hover_color="#2e1818",
                      font=("Arial", 10), command=self._clear_transcript).pack(side="left")

        analyze_row = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        analyze_row.pack(fill="x", padx=8, pady=(4,8))
        ctk.CTkButton(analyze_row, text="🧠 Analyser", width=130, height=30,
                      fg_color="#2a1a4a", hover_color="#3a2a5a",
                      font=("Arial", 10, "bold"), command=self._run_analysis).pack(side="left", padx=(0,4))
        ctk.CTkButton(analyze_row, text="💾 Export analyse", width=120, height=30,
                      fg_color="#1a2a1a", hover_color="#2a3a2a",
                      font=("Arial", 10), command=self._export_analysis).pack(side="left")

    # ── Traduction (scrollable aussi) ────────────────────────────────────────
    def _build_tab_traduction(self, parent):
        self.trad_frame = ctk.CTkScrollableFrame(parent, fg_color="transparent")  # scrollable

        trad_header = ctk.CTkFrame(self.trad_frame, fg_color="#0d1520",
                                    corner_radius=6, border_width=1,
                                    border_color="#1e2d45")
        trad_header.pack(fill="x", padx=8, pady=(8,4))
        h_row1 = ctk.CTkFrame(trad_header, fg_color="transparent")
        h_row1.pack(fill="x", padx=8, pady=(6,2))
        ctk.CTkLabel(h_row1, text="🌍 Traduction Multilingue",
                     font=("Arial", 11, "bold"), text_color="#4a9abf").pack(side="left")
        self.trad_status_lbl = ctk.CTkLabel(h_row1, text="⬤ Inactif", font=("Arial", 9), text_color="#444")
        self.trad_status_lbl.pack(side="right")
        h_row2 = ctk.CTkFrame(trad_header, fg_color="transparent")
        h_row2.pack(fill="x", padx=8, pady=(2,2))
        ctk.CTkLabel(h_row2, text="Source :", font=("Arial", 10), text_color="#888", width=55).pack(side="left")
        self.trad_src_lbl = ctk.CTkLabel(h_row2, text="— (auto, détectée par Whisper)",
                                         font=("Arial", 10), text_color="#7ec88a")
        self.trad_src_lbl.pack(side="left", padx=(4,0))
        h_row3 = ctk.CTkFrame(trad_header, fg_color="transparent")
        h_row3.pack(fill="x", padx=8, pady=(2,6))
        ctk.CTkLabel(h_row3, text="Cible :", font=("Arial", 10), text_color="#888", width=55).pack(side="left")
        lang_options = []
        if TRANSLATION_AVAILABLE:
            for code, label, flag in SUPPORTED_LANGUAGES:
                lang_options.append(f"{flag} {label}")
        else:
            lang_options = ["🇫🇷 Français", "🇬🇧 English"]
        self._trad_lang_var = tk.StringVar(value="🇫🇷 Français")
        self.trad_lang_menu = ctk.CTkOptionMenu(
            h_row3, variable=self._trad_lang_var,
            values=lang_options, width=140, height=26,
            font=("Arial", 9), fg_color="#1a2a3a", button_color="#1e3248",
            command=self._on_trad_lang_changed)
        self.trad_lang_menu.pack(side="left", padx=(4,6))
        self.trad_toggle_btn = ctk.CTkButton(
            h_row3, text="▶ Activer", width=70, height=26,
            fg_color="#1a4a6a", hover_color="#143a54",
            font=("Arial", 9, "bold"), command=self._toggle_translation)
        self.trad_toggle_btn.pack(side="left")

        ctk.CTkLabel(self.trad_frame, text="Texte traduit :",
                     font=("Arial", 10), text_color="#555").pack(anchor="w", padx=8, pady=(6,2))
        self.trad_box = ctk.CTkTextbox(self.trad_frame, font=("Arial", 11), wrap="word", height=120)
        self.trad_box.pack(fill="x", padx=8, pady=(0,4))
        self.trad_box.insert("end", "La traduction apparaîtra ici…\n")
        self.trad_box.configure(text_color="#555")

        trad_btns = ctk.CTkFrame(self.trad_frame, fg_color="transparent")
        trad_btns.pack(fill="x", padx=8, pady=(0,8))
        ctk.CTkButton(trad_btns, text="🗑 Effacer", width=80, height=28,
                      fg_color="#2a1a1a", hover_color="#3a2020",
                      font=("Arial", 9), command=self._clear_translation).pack(side="left", padx=(0,4))
        ctk.CTkButton(trad_btns, text="💾 Exporter", width=80, height=28,
                      fg_color="#1a2a1a", hover_color="#2a3a2a",
                      font=("Arial", 9), command=self._export_translation).pack(side="left")
        self.trad_info_lbl = ctk.CTkLabel(self.trad_frame,
                                          text="ℹ Malagasy : texte original affiché (traduction non disponible offline)",
                                          font=("Arial", 9), text_color="#555")
        self.trad_info_lbl.pack(anchor="w", padx=8, pady=(0,4))

    # ── Rapport ──────────────────────────────────────────────────────────────
    def _build_tab_rapport(self, parent):
        self.rapport_frame = ctk.CTkFrame(parent, fg_color="transparent")

        ctk.CTkLabel(self.rapport_frame, text="📊 Génération de Rapport",
                     font=("Arial", 12, "bold"), text_color="#4a9abf").pack(anchor="w", padx=10, pady=(10,6))
        cfg_frame = ctk.CTkFrame(self.rapport_frame, fg_color="#0d1520",
                                  corner_radius=6, border_width=1, border_color="#1e2d45")
        cfg_frame.pack(fill="x", padx=8, pady=(0,6))
        r1 = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        r1.pack(fill="x", padx=8, pady=(6,2))
        ctk.CTkLabel(r1, text="Template :", font=("Arial", 10), text_color="#888", width=70).pack(side="left")
        self._report_template_var = tk.StringVar(value="Direction")
        ctk.CTkOptionMenu(r1, variable=self._report_template_var,
                          values=["Direction","RH","Commercial","Technique"],
                          width=130, height=24, font=("Arial", 9),
                          fg_color="#1a2a3a", button_color="#1e3248").pack(side="left", padx=(4,0))
        r2 = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        r2.pack(fill="x", padx=8, pady=(2,2))
        ctk.CTkLabel(r2, text="Niveau :", font=("Arial", 10), text_color="#888", width=70).pack(side="left")
        self._report_detail_var = tk.StringVar(value="standard")
        ctk.CTkOptionMenu(r2, variable=self._report_detail_var,
                          values=["resume","standard","complet"],
                          width=130, height=24, font=("Arial", 9),
                          fg_color="#1a2a3a", button_color="#1e3248").pack(side="left", padx=(4,0))
        r3 = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        r3.pack(fill="x", padx=8, pady=(2,2))
        ctk.CTkLabel(r3, text="Titre :", font=("Arial", 10), text_color="#888", width=70).pack(side="left")
        self.report_title_entry = ctk.CTkEntry(r3, placeholder_text="Compte-rendu de réunion",
                                               width=150, height=24, font=("Arial", 9))
        self.report_title_entry.pack(side="left", padx=(4,0))
        r4 = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        r4.pack(fill="x", padx=8, pady=(2,2))
        ctk.CTkLabel(r4, text="Société :", font=("Arial", 10), text_color="#888", width=70).pack(side="left")
        self.report_company_entry = ctk.CTkEntry(r4, placeholder_text="PolyMeet",
                                                 width=150, height=24, font=("Arial", 9))
        self.report_company_entry.pack(side="left", padx=(4,0))
        r5 = ctk.CTkFrame(cfg_frame, fg_color="transparent")
        r5.pack(fill="x", padx=8, pady=(2,6))
        ctk.CTkLabel(r5, text="MDP PDF :", font=("Arial", 10), text_color="#888", width=70).pack(side="left")
        self.report_pwd_entry = ctk.CTkEntry(r5, placeholder_text="(optionnel)",
                                             width=150, height=24, font=("Arial", 9), show="*")
        self.report_pwd_entry.pack(side="left", padx=(4,0))

        ctk.CTkLabel(self.rapport_frame, text="Sections :",
                     font=("Arial", 10), text_color="#888").pack(anchor="w", padx=10, pady=(4,2))
        sec_frame = ctk.CTkFrame(self.rapport_frame, fg_color="#0d1520", corner_radius=6)
        sec_frame.pack(fill="x", padx=8, pady=(0,6))
        self._section_vars = {}
        sections_labels = [
            ("page_garde", "📄 Page de garde"),
            ("participants", "👥 Participants"),
            ("resume_executif", "📋 Résumé exécutif"),
            ("decisions", "✅ Décisions"),
            ("taches", "📌 Tâches"),
            ("transcription", "📝 Transcription"),
            ("prochaines_etapes", "🔜 Prochaines étapes"),
        ]
        for key, label in sections_labels:
            var = tk.BooleanVar(value=(key != "transcription"))
            self._section_vars[key] = var
            ctk.CTkCheckBox(sec_frame, text=label, variable=var,
                            font=("Arial", 9), height=22).pack(anchor="w", padx=10, pady=1)

        self.report_status_lbl = ctk.CTkLabel(self.rapport_frame, text="",
                                              font=("Arial", 9), text_color="#4a9abf")
        self.report_status_lbl.pack(anchor="w", padx=10, pady=(4,2))
        gen_row = ctk.CTkFrame(self.rapport_frame, fg_color="transparent")
        gen_row.pack(fill="x", padx=8, pady=(0,4))
        ctk.CTkButton(gen_row, text="📄 Word", width=70, height=30,
                      fg_color="#1a3a6a", command=self._generate_word).pack(side="left", padx=(0,4))
        ctk.CTkButton(gen_row, text="📑 PDF", width=70, height=30,
                      fg_color="#4a1a1a", command=self._generate_pdf).pack(side="left", padx=(0,4))
        ctk.CTkButton(gen_row, text="📄+📑 Les deux", width=100, height=30,
                      fg_color="#1a3a2a", command=self._generate_both).pack(side="left")

    # ── Enregistrement ──────────────────────────────────────────────────────
    def _build_recording_panel(self):
        rec_panel = ctk.CTkFrame(self, height=62, fg_color="#0e1520",
                                 corner_radius=0, border_width=1, border_color="#1e2d45")
        rec_panel.pack(fill="x", side="bottom")
        rec_panel.pack_propagate(False)
        ctk.CTkLabel(rec_panel, text="⏺  ENREGISTREMENT",
                     font=("Arial", 10, "bold"), text_color="#4a7abf").pack(side="left", padx=(14,10))
        self.rec_start_btn = ctk.CTkButton(rec_panel, text="▶  Démarrer", width=118, height=36,
                                           fg_color="#1a6b3a", hover_color="#145430",
                                           font=("Arial", 11, "bold"), command=self._rec_start)
        self.rec_start_btn.pack(side="left", padx=(0,6), pady=12)
        self.rec_pause_btn = ctk.CTkButton(rec_panel, text="⏸  Pause", width=110, height=36,
                                           fg_color="#555", hover_color="#666",
                                           font=("Arial", 11, "bold"), command=self._rec_pause_resume,
                                           state="disabled")
        self.rec_pause_btn.pack(side="left", padx=(0,6), pady=12)
        self.rec_stop_btn = ctk.CTkButton(rec_panel, text="⏹  Arrêter", width=110, height=36,
                                          fg_color="#7a2020", hover_color="#5e1818",
                                          font=("Arial", 11, "bold"), command=self._rec_stop,
                                          state="disabled")
        self.rec_stop_btn.pack(side="left", padx=(0,14), pady=12)
        sep = ctk.CTkFrame(rec_panel, width=2, height=36, fg_color="#1e2d45")
        sep.pack(side="left", padx=(0,14))
        self.rec_status_dot = ctk.CTkLabel(rec_panel, text="⬤", font=("Arial", 14), text_color="#333")
        self.rec_status_dot.pack(side="left", padx=(0,4))
        self.rec_status_lbl = ctk.CTkLabel(rec_panel, text="Inactif", font=("Arial", 11), text_color="#555")
        self.rec_status_lbl.pack(side="left", padx=(0,16))
        ctk.CTkLabel(rec_panel, text="⏱", font=("Arial", 13)).pack(side="left", padx=(0,4))
        self.rec_duration_lbl = ctk.CTkLabel(rec_panel, text="00:00", font=("Arial", 14, "bold"),
                                            text_color="#4a7abf")
        self.rec_duration_lbl.pack(side="left", padx=(0,16))
        self.rec_autosave_lbl = ctk.CTkLabel(rec_panel, text="", font=("Arial", 10), text_color="#4a9a4a")
        self.rec_autosave_lbl.pack(side="left", padx=(0,10))
        ctk.CTkLabel(rec_panel, text="Ctrl+R  Démarrer   Ctrl+P  Pause   Ctrl+S  Arrêter",
                     font=("Arial", 9), text_color="#333").pack(side="right", padx=14)

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
            self.trad_toggle_btn.configure(text="▶ Activer", fg_color="#1a4a6a")
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

    # ──────────────────────────────────────────────────────────────────────────
    # [F-03] Méthodes de transcription
    # ──────────────────────────────────────────────────────────────────────────
    def _toggle_transcription(self):
        if not TRANSCRIPTION_AVAILABLE:
            msgbox.showerror("Module manquant",
                             "transcription_engine.py introuvable.")
            return
        if self.transcription_engine is None:
            engine_check = TranscriptionEngine()
            if not engine_check.is_model_cached():
                model_name = "small"
                reponse = msgbox.askyesno(
                    "Téléchargement requis",
                    f"Le modèle Whisper '{model_name}' doit être téléchargé.\nContinuer ?")
                if not reponse: return
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
                self.trans_status_lbl.configure(text="⬤ Actif", text_color="#4caf50")
                self.trans_box.delete("1.0", "end")
                self.trans_box.configure(text_color="white")
                self._add_chat_line("📝 Transcription activée (Whisper small)")
            else:
                self.transcription_engine = None
                self.trans_toggle_btn.configure(text="▶ Activer", state="normal",
                                                fg_color="#1a4a6a")
                self.trans_status_lbl.configure(text="⬤ Erreur", text_color="#e05050")
                msgbox.showerror("Erreur", "Impossible de charger le modèle Whisper.")
        else:
            self.transcription_engine.stop()
            self.transcription_engine = None
            self.trans_toggle_btn.configure(text="▶ Activer", fg_color="#1a4a6a")
            self.trans_status_lbl.configure(text="⬤ Inactif", text_color="#444")
            self._add_chat_line("📝 Transcription désactivée")

    def _on_record_chunk_and_transcribe(self, raw_bytes: bytes):
        self.recording_engine.add_chunk(raw_bytes)
        if self.transcription_engine and self.transcription_engine.is_running:
            whisper_chunk = (self.audio_engine.resample_to_whisper(raw_bytes)
                             if self.audio_engine else raw_bytes)
            self.transcription_engine.feed(whisper_chunk)
        if (self.diarization_engine and
                self.diarization_engine._mode == DiarizationEngine.MODE_PRESENTIEL
                if DIARIZATION_AVAILABLE else False):
            self.diarization_engine.feed_local_audio(raw_bytes)

    def _on_transcript_segment(self, text: str, language: str, timestamp: str,
                               speaker_label: str = "?", speaker_color: str = "#aaa"):
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

            # Stocker pour le rapport
            self._transcript_entries.append({
                "time": timestamp, "lang": language, "text": text,
                "speaker_label": speaker_label, "speaker_color": speaker_color
            })

            # ── Envoyer à la traduction si active ────────────────────────
            if self.translation_engine and self.translation_engine.is_ready:
                self.translation_engine.set_source_language(language)
                if language != self.translation_engine.target_language:
                    self.translation_engine.translate(text, source_lang=language)

        self.after(0, _update)

    def _export_transcript(self):
        text = ""
        if self.transcription_engine:
            text = self.transcription_engine.get_full_transcript()
        if not text:
            text = self.trans_box.get("1.0", "end").strip()
        if not text:
            msgbox.showinfo("Export", "Aucun texte à exporter.")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RECORDINGS_DIR, f"transcription_{ts}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            msgbox.showinfo("Export réussi", f"Fichier : {path}")
        except Exception as e:
            msgbox.showerror("Erreur", str(e))

    def _clear_transcript(self):
        if msgbox.askyesno("Effacer", "Effacer toute la transcription ?"):
            self.trans_box.delete("1.0", "end")
            if self.transcription_engine:
                self.transcription_engine.clear_transcript()
            self.trans_lang_lbl.configure(text="—")
            self._transcript_entries.clear()

    # ──────────────────────────────────────────────────────────────────────────
    # [F-04] Diarisation — gestion des speakers
    # ──────────────────────────────────────────────────────────────────────────
    def _start_diarization(self, mode: str):
        if not DIARIZATION_AVAILABLE:
            return
        if self.diarization_engine:
            return
        self.diarization_engine = DiarizationEngine(
            on_speaker_change=self._on_speaker_change,
            on_speakers_updated=self._on_speakers_updated)
        self.diarization_engine.start(mode=mode)

    def _stop_diarization(self):
        if self.diarization_engine:
            self.diarization_engine.stop()
            self.diarization_engine = None

    def _on_speaker_change(self, speaker, timestamp: str):
        def _update():
            if self.transcription_engine:
                self.transcription_engine.set_current_speaker(
                    label=speaker.label, color=speaker.color)
            self._highlight_active_speaker(speaker.auto_name)
        self.after(0, _update)

    def _on_speakers_updated(self, speakers: dict):
        def _update():
            self._refresh_speaker_badges(speakers)
        self.after(0, _update)

    def _refresh_speaker_badges(self, speakers: dict):
        current_keys = set(speakers.keys())
        for key in list(self._speaker_badges.keys()):
            if key not in current_keys:
                self._speaker_badges[key].destroy()
                del self._speaker_badges[key]
        for auto_name, profile in speakers.items():
            if auto_name not in self._speaker_badges:
                self._create_speaker_badge(auto_name, profile)
            else:
                badge = self._speaker_badges[auto_name]
                try:
                    badge._name_lbl.configure(text=profile.label)
                    badge._count_lbl.configure(text=f"{profile.speech_count} int.")
                except Exception:
                    pass
        count = len(speakers)
        self.spk_count_lbl.configure(
            text=f"{count} speaker{'s' if count > 1 else ''}")

    def _create_speaker_badge(self, auto_name: str, profile):
        badge = tk.Frame(self.spk_list_frame, bg="#0d1117",
                         relief="flat", bd=0)
        badge.pack(fill="x", padx=2, pady=2)
        dot = tk.Label(badge, text="⬤", bg="#0d1117",
                       fg=profile.color, font=("Arial", 10))
        dot.pack(side="left", padx=(4, 2))
        name_lbl = tk.Label(badge, text=profile.label,
                            bg="#0d1117", fg="white",
                            font=("Arial", 10, "bold"),
                            cursor="hand2")
        name_lbl.pack(side="left", padx=(0, 6))
        name_lbl.bind("<Double-Button-1>",
                      lambda e, k=auto_name: self._rename_speaker_dialog(k))
        count_lbl = tk.Label(badge, text=f"{profile.speech_count} int.",
                             bg="#0d1117", fg="#555", font=("Arial", 9))
        count_lbl.pack(side="left")
        badge._name_lbl  = name_lbl
        badge._count_lbl = count_lbl
        badge._dot       = dot
        badge._is_active = False
        self._speaker_badges[auto_name] = badge

    def _highlight_active_speaker(self, auto_name: str):
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
        if not self.diarization_engine:
            return
        dlg = tk.Toplevel(self)
        dlg.title("Renommer le participant")
        dlg.geometry("320x130")
        dlg.configure(bg="#0d1117")
        dlg.resizable(False, False)
        dlg.grab_set()
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

    # ──────────────────────────────────────────────────────────────────────────
    # [F-05/F-06] Traduction Multilingue
    # ──────────────────────────────────────────────────────────────────────────
    def _get_target_lang_code(self) -> str:
        if not TRANSLATION_AVAILABLE:
            return "fr"
        label = self._trad_lang_var.get()
        for code, name, flag in SUPPORTED_LANGUAGES:
            if name in label or flag in label:
                return code
        return "fr"

    def _on_trad_lang_changed(self, choice: str):
        lang_code = self._get_target_lang_code()
        if TRANSLATION_AVAILABLE and self.translation_engine:
            src = self.translation_engine.source_language
            available_targets = self.translation_engine.get_available_targets(src)
            new_options = [f"{flag} {name}" for code, name, flag in available_targets]
            self.trad_lang_menu.configure(values=new_options)
            self.trad_status_lbl.configure(text="⬤ Changement…", text_color="#e0a030")
            self.translation_engine.set_target_language(lang_code)
        lang_label = LANG_NAMES.get(lang_code, lang_code.upper()) if TRANSLATION_AVAILABLE else lang_code
        print(f"[Translation] Cible : {lang_label}")

    def _toggle_translation(self):
        if not TRANSLATION_AVAILABLE:
            msgbox.showerror("Module manquant", "translation_engine.py introuvable.")
            return
        if self.translation_engine is None:
            lang_code = self._get_target_lang_code()
            self.trad_toggle_btn.configure(text="⏳", state="disabled")
            self.trad_status_lbl.configure(text="⬤ Démarrage…", text_color="#e0a030")
            self.translation_engine = TranslationEngine(
                on_translated=self._on_translated,
                on_pack_ready=self._on_translation_pack_ready)
            detected_src = "auto"
            if self.transcription_engine and hasattr(self.transcription_engine, "_last_lang"):
                detected_src = self.transcription_engine._last_lang or "auto"
            ok = self.translation_engine.start(source_lang=detected_src, target_lang=lang_code)
            if ok:
                self.trad_box.delete("1.0", "end")
                self.trad_box.configure(text_color="white")
                self._add_chat_line(f"🌍 Traduction activée → {lang_code.upper()}")
            else:
                self.translation_engine = None
                self.trad_toggle_btn.configure(text="▶ Activer", state="normal",
                                               fg_color="#1a4a6a")
                self.trad_status_lbl.configure(text="⬤ Erreur", text_color="#e05050")
                msgbox.showerror("Erreur", "Impossible de démarrer la traduction.")
        else:
            self.translation_engine.stop()
            self.translation_engine = None
            self.trad_toggle_btn.configure(text="▶ Activer", fg_color="#1a4a6a")
            self.trad_status_lbl.configure(text="⬤ Inactif", text_color="#444")
            self._add_chat_line("🌍 Traduction désactivée")

    def _on_translation_pack_ready(self, lang_code: str):
        def _update():
            label = LANG_NAMES.get(lang_code, lang_code.upper())
            self.trad_status_lbl.configure(text=f"⬤ Actif ({label})", text_color="#4caf50")
            self.trad_toggle_btn.configure(text="⏹ OFF", state="normal",
                                           fg_color="#6a1a1a", hover_color="#541414")
            if lang_code == "mg":
                self.trad_box.insert("end",
                    "ℹ Malagasy : texte original affiché (traduction non disponible offline)\n")
        self.after(0, _update)

    def _on_translated(self, original: str, translated: str,
                       source_lang: str, target_lang: str):
        def _update():
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            line = f"[{ts}] {translated}\n"
            self.trad_box.configure(state="normal")
            self.trad_box.insert("end", line)
            self.trad_box.see("end")
            if self._active_tab.get() == "chat":
                self.tab_trad_btn.configure(text="🌍 Traduction ●")
        self.after(0, _update)

    def _clear_translation(self):
        self.trad_box.delete("1.0", "end")

    def _export_translation(self):
        text = self.trad_box.get("1.0", "end").strip()
        if not text:
            msgbox.showinfo("Export", "Aucune traduction.")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RECORDINGS_DIR, f"traduction_{ts}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            msgbox.showinfo("Export réussi", f"Fichier : {path}")
        except Exception as e:
            msgbox.showerror("Erreur", str(e))

    # ──────────────────────────────────────────────────────────────────────────
    # [F-07/F-08] Analyse IA & Résumé Intelligent
    # ──────────────────────────────────────────────────────────────────────────
    def _run_analysis(self):
        if not ANALYSIS_AVAILABLE:
            msgbox.showerror("Module manquant", "analysis_engine.py introuvable.")
            return
        transcript = ""
        if self.transcription_engine:
            transcript = self.transcription_engine.get_full_transcript()
        if not transcript:
            transcript = self.trans_box.get("1.0", "end").strip()
        if not transcript or len(transcript) < 20:
            msgbox.showwarning("Transcription vide",
                               "Il faut d'abord transcrire la réunion.")
            return
        speakers = list(self.tiles.keys()) if self.tiles else []
        def _do_analysis():
            analysis = self.analysis_engine.analyze(transcript, speakers=speakers)
            analysis.participants = list(self.tiles.keys())  # ajout pour le rapport
            self._last_analysis = analysis
            self.after(0, lambda: self._show_analysis_result(analysis))
        threading.Thread(target=_do_analysis, daemon=True).start()

    def _show_analysis_result(self, analysis):
        win = tk.Toplevel(self)
        win.title("🧠 Analyse de la réunion")
        win.geometry("720x600")
        win.configure(bg="#0d1117")
        text_area = ctk.CTkTextbox(win, font=("Courier", 10), wrap="word")
        text_area.pack(fill="both", expand=True, padx=8, pady=8)
        report = self.analysis_engine.format_analysis_report(analysis)
        text_area.insert("end", report)
        text_area.configure(state="disabled")
        btn_row = ctk.CTkFrame(win, fg_color="#161b22", height=44, corner_radius=0)
        btn_row.pack(fill="x", side="bottom")
        ctk.CTkButton(btn_row, text="📋 Copier", width=90, height=30,
                      command=lambda: (win.clipboard_append(report),
                                       msgbox.showinfo("Copié", "Rapport copié !"))
                      ).pack(side="left", padx=10, pady=7)
        ctk.CTkButton(btn_row, text="✕ Fermer", width=80, height=30,
                      fg_color="#3a2020", hover_color="#2e1818",
                      command=win.destroy).pack(side="right", padx=10, pady=7)

    def _export_analysis(self, report: str = ""):
        if not report and self._last_analysis:
            report = self.analysis_engine.format_analysis_report(
                self._last_analysis)
        if not report:
            msgbox.showwarning("Aucune analyse", "Lancez d'abord une analyse.")
            return
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(RECORDINGS_DIR, f"analyse_{ts}.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(report)
            msgbox.showinfo("Export réussi", f"Fichier : {path}")
        except Exception as e:
            msgbox.showerror("Erreur", str(e))

    # ──────────────────────────────────────────────────────────────────────────
    # [F-09/F-10/F-11] Génération de rapports (Word/PDF)
    # ──────────────────────────────────────────────────────────────────────────
    def _build_report_config(self):
        config = ReportConfig()
        config.template   = self._report_template_var.get()
        config.detail     = self._report_detail_var.get()
        config.title      = self.report_title_entry.get() or "Compte-rendu"
        config.company    = self.report_company_entry.get() or "PolyMeet"
        config.password   = self.report_pwd_entry.get()
        sections = []
        for key, var in self._section_vars.items():
            if var.get():
                sections.append(key)
        config.sections = sections
        # S'assurer qu'il y a une analyse
        if not self._last_analysis:
            if self.analysis_engine:
                transcript = self.trans_box.get("1.0", "end").strip()
                if transcript:
                    analysis = self.analysis_engine.analyze(
                        transcript, speakers=list(self.tiles.keys()))
                    analysis.participants = list(self.tiles.keys())
                    self._last_analysis = analysis
                else:
                    self._last_analysis = MeetingAnalysis()
            else:
                self._last_analysis = MeetingAnalysis()
        return config

    def _generate_word(self):
        if not REPORT_AVAILABLE:
            msgbox.showerror("Module manquant", "report_engine.py introuvable.")
            return
        config = self._build_report_config()
        entries = self._transcript_entries if "transcription" in config.sections else []
        path = self.report_engine.generate_docx(self._last_analysis, entries, config)
        if path:
            self.report_status_lbl.configure(text=f"✅ Word : {os.path.basename(path)}")
            self._add_chat_line(f"📄 Rapport Word : {path}")

    def _generate_pdf(self):
        if not REPORT_AVAILABLE:
            msgbox.showerror("Module manquant", "report_engine.py introuvable.")
            return
        config = self._build_report_config()
        entries = self._transcript_entries if "transcription" in config.sections else []
        path = self.report_engine.generate_pdf(self._last_analysis, entries, config)
        if path:
            self.report_status_lbl.configure(text=f"✅ PDF : {os.path.basename(path)}")
            self._add_chat_line(f"📑 Rapport PDF : {path}")

    def _generate_both(self):
        self._generate_word()
        self._generate_pdf()

    def on_closing(self):
        self._leave()
        self.destroy()


if __name__ == "__main__":
    app = VideoCallApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()