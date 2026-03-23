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
AUDIO_RATE      = 16000
AUDIO_CHANNELS  = 1
AUDIO_FORMAT    = pyaudio.paInt16
AUDIO_CHUNK     = 1024
AUDIO_VAD_RMS   = 300
AUDIO_QUEUE_MAX = 8

# ─── Paramètres enregistrement [F-02] ───────────────────────────────────────
RECORDINGS_DIR      = "recordings"
AUTOSAVE_INTERVAL_S = 300          # 5 minutes
os.makedirs(RECORDINGS_DIR, exist_ok=True)


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
    def __init__(self, on_chunk_ready, on_record_chunk=None):
        self.on_chunk_ready  = on_chunk_ready
        self.on_record_chunk = on_record_chunk   # callback [F-02]
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

        try:
            self.in_stream = self.pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                input=True,
                frames_per_buffer=AUDIO_CHUNK,
                stream_callback=self._capture_callback
            )
            self.in_stream.start_stream()
        except Exception as e:
            print(f"[Audio] ❌ Micro : {e}")
            return False

        try:
            self.out_stream = self.pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                output=True,
                frames_per_buffer=AUDIO_CHUNK
            )
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

    def _capture_callback(self, in_data, frame_count, time_info, status):
        if in_data:
            # Toujours envoyer au recorder même si muté [F-02]
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

        self.audio_engine:         AudioEngine    | None = None
        self.recording_engine:     RecordingEngine        = RecordingEngine()
        self.transcription_engine                         = None  # [F-03]

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
        self.mic_status.pack(side="left", padx=(0, 6))

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

        # Status langue détectée
        trans_top = ctk.CTkFrame(self.trans_frame, fg_color="transparent")
        trans_top.pack(fill="x", padx=8, pady=(6, 2))

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
                # Effacer le placeholder
                self.trans_box.delete("1.0", "end")
                self.trans_box.configure(text_color="white")
                self._add_chat_line("📝 Transcription activée (Whisper small)")
            else:
                self.transcription_engine = None
                self.trans_toggle_btn.configure(
                    text="▶ Activer", state="normal",
                    fg_color="#1a4a6a", hover_color="#143a54")
                self.trans_status_lbl.configure(
                    text="⬤ Erreur", text_color="#e05050")
        else:
            # Arrêter
            self.transcription_engine.stop()
            self.transcription_engine = None
            self.trans_toggle_btn.configure(
                text="▶ Activer", state="normal",
                fg_color="#1a4a6a", hover_color="#143a54")
            self.trans_status_lbl.configure(text="⬤ Inactif", text_color="#444")
            self._add_chat_line("📝 Transcription désactivée")

    def _on_transcript_segment(self, text: str, language: str, timestamp: str):
        """
        Callback appelé par TranscriptionEngine (thread Whisper).
        On repasse dans le thread UI via after().
        """
        def _update():
            # Langue
            lang_names = {
                "fr": "Français 🇫🇷", "en": "English 🇬🇧",
                "mg": "Malagasy 🇲🇬", "ar": "Arabe 🇸🇦",
                "es": "Espagnol 🇪🇸", "de": "Allemand 🇩🇪",
                "zh": "Chinois 🇨🇳", "pt": "Portugais 🇵🇹",
                "it": "Italien 🇮🇹",
            }
            lang_display = lang_names.get(language, language.upper())
            self.trans_lang_lbl.configure(text=lang_display)

            # Ajouter le texte dans la zone de transcription
            line = f"[{timestamp}]  {text}\n"
            self.trans_box.insert("end", line)
            self.trans_box.see("end")

            # Basculer automatiquement sur l'onglet transcription
            # si l'utilisateur est sur chat (discret : seulement 1 fois)
            if self._active_tab.get() == "chat":
                self.tab_trans_btn.configure(text="📝 Transcription ●")

        self.after(0, _update)

    def _on_record_chunk_and_transcribe(self, raw_bytes: bytes):
        """
        Callback unifié : envoie le chunk à RecordingEngine ET à
        TranscriptionEngine si actif.
        """
        self.recording_engine.add_chunk(raw_bytes)
        if self.transcription_engine and self.transcription_engine.is_running:
            self.transcription_engine.feed(raw_bytes)

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

        # AudioEngine avec callbacks recorder + transcription [F-02/F-03]
        self.audio_engine = AudioEngine(
            on_chunk_ready=self._on_audio_chunk_ready,
            on_record_chunk=self._on_record_chunk_and_transcribe)
        ok = self.audio_engine.start()
        if ok:
            self.mic_status.configure(text="🎤 Actif ✅", text_color="#4caf50")
            self.mute_btn.configure(state="normal")
        else:
            self.mic_status.configure(text="🎤 Erreur", text_color="#e07050")

        self.my_tile = self._add_tile(name, is_me=True)
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
            # Horodatage arrivée [F-02]
            self.after(0, lambda n=n: self.recording_engine.log_intervention(n, "A rejoint la réunion"))
        elif t == "user_left":
            n = data["name"]
            self.after(0, lambda n=n: self._remove_tile(n))
            self.after(0, lambda n=n: self._add_chat_line(f"👋 {n} a quitté"))
            self.after(0, lambda n=n: self.recording_engine.log_intervention(n, "A quitté la réunion"))
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
                # Horodatage prise de parole [F-02]
                self.recording_engine.log_intervention(name, "Prise de parole")
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
