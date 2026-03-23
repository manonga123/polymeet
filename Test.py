"""
client.py  –  Client de salle vidéo + audio partagée
─────────────────────────────────────────────────────
Fonctionnalités :
  • Vidéo multi-participants (grille dynamique)
  • Audio temps réel bidirectionnel (micro → tous les autres)
  • VAD (Voice Activity Detection) simple par seuil RMS
  • Bouton Mute / Unmute dans l'UI
  • Queue de lecture par participant (anti-accumulation)
  • Thread de playback mixé pour tous les participants
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
from collections import deque

warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ─── Paramètres réseau ──────────────────────────────────────────────────────
DROIDCAM_IP   = "192.168.43.102"
DROIDCAM_PORT = 4747
SERVER_IP     = "192.168.43.156"
SERVER_PORT   = 9765

# ─── Paramètres vidéo ───────────────────────────────────────────────────────
FRAME_QUALITY    = 50
FRAME_RESIZE     = (320, 240)
FRAME_INTERVAL_MS = 50        # ~20 fps

# ─── Paramètres audio ───────────────────────────────────────────────────────
AUDIO_RATE       = 16000      # Hz — bon compromis qualité/bande passante
AUDIO_CHANNELS   = 1          # Mono (stéréo inutile pour la voix)
AUDIO_FORMAT     = pyaudio.paInt16   # 16 bits PCM
AUDIO_CHUNK      = 1024       # ~64ms à 16kHz → latence acceptable
AUDIO_VAD_RMS    = 300        # Seuil RMS sous lequel on considère silence
AUDIO_QUEUE_MAX  = 8          # Taille max de la queue par participant


def find_camera_source():
    url = f"http://{DROIDCAM_IP}:{DROIDCAM_PORT}/mjpegfeed"
    cap = cv2.VideoCapture(url)
    if cap.isOpened():
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            print(f"[Camera] DroidCam trouvé : {url}")
            return url, "📱 DroidCam"
    for idx in range(3):
        cap = cv2.VideoCapture(idx)
        if cap.isOpened():
            ret, frame = cap.read()
            cap.release()
            if ret and frame is not None:
                print(f"[Camera] Webcam locale index {idx}")
                return idx, f"💻 Webcam ({idx})"
    print("[Camera] Aucune caméra disponible")
    return None, None


# ─── Tuile vidéo ─────────────────────────────────────────────────────────────
class VideoTile(tk.Frame):
    """Une tuile = un participant dans la grille vidéo."""

    def __init__(self, parent, name, is_me=False, **kwargs):
        bg = "#0d1117"
        super().__init__(parent, bg=bg, **kwargs)
        self.name  = name
        self.is_me = is_me

        # Barre de nom en bas
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

        # Indicateur micro actif (petit cercle vert)
        self.mic_indicator = tk.Label(
            self.name_bar, text="🎤", bg=bar_bg,
            font=("Arial", 9), fg="#555")
        self.mic_indicator.pack(side="right", padx=4)

        # Zone image
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
        """Allume/éteint l'indicateur micro selon activité vocale."""
        if not self.winfo_exists():
            return
        color = "#4caf50" if speaking else "#555"
        self.mic_indicator.configure(fg=color)


# ─── Moteur audio ────────────────────────────────────────────────────────────
class AudioEngine:
    """
    Gère capture micro et lecture haut-parleurs.

    Architecture :
      • 1 thread capture  : lit le micro en callback PyAudio non-bloquant
      • 1 thread playback : lit les queues de tous les participants et joue le mix
      • 1 dict de queues  : audio_queues[name] = deque(maxlen=AUDIO_QUEUE_MAX)
    """

    def __init__(self, on_chunk_ready):
        """
        on_chunk_ready(b64_chunk: str) → appelé quand un chunk micro est prêt à envoyer.
        """
        self.on_chunk_ready = on_chunk_ready
        self.pa             = None
        self.in_stream      = None
        self.out_stream     = None
        self.muted          = False
        self.running        = False
        self.audio_queues   = {}    # name → deque(bytes)
        self._lock          = threading.Lock()
        self._pb_thread     = None

    # ── Démarrage ────────────────────────────────────────────────────────────
    def start(self):
        try:
            self.pa = pyaudio.PyAudio()
        except Exception as e:
            print(f"[Audio] PyAudio indisponible : {e}")
            return False

        # Stream de capture micro
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
            print("[Audio] ✅ Capture micro démarrée")
        except Exception as e:
            print(f"[Audio] ❌ Impossible d'ouvrir le micro : {e}")
            return False

        # Stream de lecture haut-parleurs (output)
        try:
            self.out_stream = self.pa.open(
                format=AUDIO_FORMAT,
                channels=AUDIO_CHANNELS,
                rate=AUDIO_RATE,
                output=True,
                frames_per_buffer=AUDIO_CHUNK
            )
            print("[Audio] ✅ Lecture audio démarrée")
        except Exception as e:
            print(f"[Audio] ❌ Impossible d'ouvrir le HP : {e}")
            return False

        self.running = True
        self._pb_thread = threading.Thread(
            target=self._playback_loop, daemon=True)
        self._pb_thread.start()
        return True

    # ── Arrêt ─────────────────────────────────────────────────────────────────
    def stop(self):
        self.running = False
        for stream in (self.in_stream, self.out_stream):
            if stream:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
        self.in_stream  = None
        self.out_stream = None
        if self.pa:
            try:
                self.pa.terminate()
            except Exception:
                pass
            self.pa = None
        with self._lock:
            self.audio_queues.clear()
        print("[Audio] Arrêté.")

    # ── Mute / Unmute ─────────────────────────────────────────────────────────
    def toggle_mute(self):
        self.muted = not self.muted
        return self.muted

    # ── Recevoir un chunk d'un participant distant ────────────────────────────
    def receive_chunk(self, name: str, b64_chunk: str):
        """Appelé depuis le thread réseau quand un chunk audio arrive."""
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

    # ── Callback de capture micro ─────────────────────────────────────────────
    def _capture_callback(self, in_data, frame_count, time_info, status):
        """Appelé par PyAudio dans son propre thread interne."""
        if not self.muted and in_data:
            # VAD : calculer RMS
            samples = np.frombuffer(in_data, dtype=np.int16).astype(np.float32)
            rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) > 0 else 0.0

            if rms > AUDIO_VAD_RMS:
                b64 = base64.b64encode(in_data).decode()
                self.on_chunk_ready(b64)

        return (None, pyaudio.paContinue)

    # ── Thread de lecture (playback) ─────────────────────────────────────────
    def _playback_loop(self):
        """
        Lit les queues de tous les participants et les mixe en un seul flux.
        S'il n'y a rien à jouer, on envoie du silence pour éviter les saccades.
        """
        silence = b'\x00' * (AUDIO_CHUNK * 2)   # int16 → 2 octets/sample

        while self.running:
            mixed = None

            with self._lock:
                names = list(self.audio_queues.keys())

            for name in names:
                with self._lock:
                    q = self.audio_queues.get(name)
                    if q and len(q) > 0:
                        chunk = q.popleft()
                    else:
                        chunk = None

                if chunk and len(chunk) == AUDIO_CHUNK * 2:
                    arr = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    if mixed is None:
                        mixed = arr
                    else:
                        mixed = mixed + arr   # sommation des flux

            if mixed is not None:
                # Clipping pour rester dans int16
                mixed = np.clip(mixed, -32768, 32767).astype(np.int16)
                data = mixed.tobytes()
            else:
                data = silence

            if self.out_stream and self.running:
                try:
                    self.out_stream.write(data)
                except Exception:
                    pass   # stream fermé pendant l'arrêt


# ─── Application principale ──────────────────────────────────────────────────
class VideoCallApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("📹 Salle Vidéo + Audio Partagée")
        self.geometry("1200x750")
        self.minsize(800, 550)
        self.resizable(True, True)

        self.ws          = None
        self.loop        = None
        self.net_thread  = None
        self.call_active = False
        self.cap         = None
        self.cam_source  = None
        self.frame_job   = None
        self.my_name     = "Moi"

        # Moteur audio (initialisé au join)
        self.audio_engine: AudioEngine | None = None

        self.tiles   = {}
        self.my_tile = None

        self._build_ui()
        self.bind("<Configure>", lambda e: self.after(100, self._relayout_grid))

    # ─── Construction de l'interface ────────────────────────────────────────
    def _build_ui(self):
        # ── Barre du haut ──────────────────────────────────────────────────
        topbar = ctk.CTkFrame(self, height=52, fg_color="#161b22", corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        ctk.CTkLabel(topbar, text="🎥 Salle Vidéo + Audio",
                     font=("Arial", 18, "bold")).pack(side="left", padx=18)

        self.status_lbl = ctk.CTkLabel(
            topbar, text="⬤ Déconnecté",
            font=("Arial", 12), text_color="#888")
        self.status_lbl.pack(side="right", padx=18)

        self.members_lbl = ctk.CTkLabel(
            topbar, text="👥 0 participant(s)",
            font=("Arial", 11), text_color="#aaa")
        self.members_lbl.pack(side="right", padx=10)

        # ── Barre de formulaire ────────────────────────────────────────────
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
        self.cam_status.pack(side="left", padx=(0, 6))

        self.mic_status = ctk.CTkLabel(
            form, text="🎤 —", font=("Arial", 10), text_color="#888")
        self.mic_status.pack(side="left", padx=(0, 10))

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

        # Bouton Mute
        self.mute_btn = ctk.CTkButton(
            form, text="🎤 Mute", width=84, height=32,
            fg_color="#444", hover_color="#555",
            font=("Arial", 11, "bold"),
            command=self._toggle_mute, state="disabled")
        self.mute_btn.pack(side="left")

        # ── Zone centrale : grille + chat ──────────────────────────────────
        center = ctk.CTkFrame(self, fg_color="transparent")
        center.pack(fill="both", expand=True)

        grid_outer = ctk.CTkFrame(center, fg_color="#0d1117", corner_radius=0)
        grid_outer.pack(side="left", fill="both", expand=True)

        self.grid_canvas = tk.Frame(grid_outer, bg="#0d1117")
        self.grid_canvas.pack(fill="both", expand=True, padx=4, pady=4)

        chat_panel = ctk.CTkFrame(center, width=270, fg_color="#161b22",
                                   corner_radius=0)
        chat_panel.pack(side="right", fill="y")
        chat_panel.pack_propagate(False)

        ctk.CTkLabel(chat_panel, text="💬 Chat",
                     font=("Arial", 13, "bold")).pack(pady=(10, 4))

        self.chat_box = ctk.CTkTextbox(
            chat_panel, font=("Arial", 11), wrap="word")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.chat_box.configure(state="disabled")

        chat_row = ctk.CTkFrame(chat_panel, fg_color="transparent")
        chat_row.pack(fill="x", padx=8, pady=(0, 10))
        self.chat_entry = ctk.CTkEntry(
            chat_row, placeholder_text="Message…", height=32)
        self.chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.chat_entry.bind("<Return>", lambda e: self._send_chat())
        ctk.CTkButton(
            chat_row, text="↩", width=34, height=32,
            command=self._send_chat).pack(side="left")

    # ─── Grille dynamique ───────────────────────────────────────────────────
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
            row = idx // cols
            col = idx %  cols
            x   = gap + col * (tw + gap)
            y   = gap + row * (th + gap)
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

    # ─── Connexion ──────────────────────────────────────────────────────────
    def _join(self):
        name   = self.name_entry.get().strip() or "Anonyme"
        room   = self.room_entry.get().strip()  or "general"
        srv_ip = self.server_entry.get().strip() or SERVER_IP
        uri    = f"ws://{srv_ip}:{SERVER_PORT}"

        self.my_name = name

        # Détection caméra
        self.cam_status.configure(text="📷 Recherche…", text_color="#aaa")
        self.update()
        src, lbl = find_camera_source()
        if src is None:
            self.cam_status.configure(text="📷 Aucune caméra", text_color="#e07050")
            msgbox.showwarning("Caméra",
                               "Aucune caméra détectée.\n"
                               "DroidCam lancé ou webcam branchée ?")
        else:
            self.cam_status.configure(text=f"📷 {lbl}", text_color="#7ec88a")
        self.cam_source = src

        # Initialiser le moteur audio
        self.audio_engine = AudioEngine(on_chunk_ready=self._on_audio_chunk_ready)
        ok = self.audio_engine.start()
        if ok:
            self.mic_status.configure(text="🎤 Actif ✅", text_color="#4caf50")
            self.mute_btn.configure(state="normal")
        else:
            self.mic_status.configure(text="🎤 Erreur", text_color="#e07050")

        # Tile locale
        self.my_tile = self._add_tile(name, is_me=True)

        # Thread réseau
        self.loop = asyncio.new_event_loop()
        self.net_thread = threading.Thread(
            target=self._run_network,
            args=(uri, name, room),
            daemon=True)
        self.net_thread.start()
        self.join_btn.configure(state="disabled")

    def _run_network(self, uri, name, room):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._connect(uri, name, room))

    async def _connect(self, uri, name, room):
        try:
            async with websockets.connect(
                uri,
                max_size=10_000_000,
                ping_interval=20,
                ping_timeout=30
            ) as ws:
                self.ws = ws
                await ws.send(json.dumps(
                    {"type": "join", "name": name, "room": room}))
                self.after(0, self._start_capture)
                async for raw in ws:
                    self._on_message(raw)
        except Exception as e:
            self.after(0, lambda err=str(e): self._on_disconnect(err))

    # ─── Envoi d'un chunk audio depuis le callback PyAudio ─────────────────
    def _on_audio_chunk_ready(self, b64_chunk: str):
        """Appelé depuis le thread PyAudio — pousse dans le loop asyncio."""
        if self.ws and self.loop and not self.loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                self.ws.send(json.dumps({
                    "type":  "audio",
                    "chunk": b64_chunk
                })),
                self.loop
            )

    # ─── Mute ───────────────────────────────────────────────────────────────
    def _toggle_mute(self):
        if not self.audio_engine:
            return
        muted = self.audio_engine.toggle_mute()
        if muted:
            self.mute_btn.configure(text="🔇 Unmute", fg_color="#b03030",
                                    hover_color="#8a2020")
            self.mic_status.configure(text="🎤 Muté", text_color="#e07050")
        else:
            self.mute_btn.configure(text="🎤 Mute", fg_color="#444",
                                    hover_color="#555")
            self.mic_status.configure(text="🎤 Actif ✅", text_color="#4caf50")

    # ─── Réception des messages WebSocket ───────────────────────────────────
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

        elif t == "user_left":
            n = data["name"]
            self.after(0, lambda n=n: self._remove_tile(n))
            self.after(0, lambda n=n: self._add_chat_line(f"👋 {n} a quitté"))

        elif t == "members":
            members = data["members"]
            self.after(0, lambda m=members: self._update_members(m))

        elif t == "video":
            name  = data["name"]
            frame = data["frame"]
            self.after(0, lambda n=name, f=frame: self._show_remote_frame(n, f))

        elif t == "audio":
            # ← NOUVEAU : recevoir un chunk audio d'un participant
            name  = data["name"]
            chunk = data.get("chunk", "")
            if chunk and self.audio_engine:
                # Envoyer le chunk au moteur audio (thread-safe via deque)
                self.audio_engine.receive_chunk(name, chunk)
                # Allumer l'indicateur micro sur la tuile
                self.after(0, lambda n=name: self._set_speaking(n, True))
                # Éteindre l'indicateur après 400ms de silence estimé
                self.after(400, lambda n=name: self._set_speaking(n, False))

        elif t == "chat":
            line = (f"[{data.get('time', '')}] "
                    f"{data.get('name', '?')} : {data.get('text', '')}")
            self.after(0, lambda l=line: self._add_chat_line(l))

    def _set_speaking(self, name: str, speaking: bool):
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
        self._stop_capture()
        self._stop_audio()
        if reason:
            self._add_chat_line(f"⚠ Déconnecté : {reason}")

    def _leave(self):
        self.call_active = False
        self._stop_capture()
        self._stop_audio()
        if self.ws and self.loop:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        for tile in list(self.tiles.values()):
            tile.destroy()
        self.tiles.clear()
        self.my_tile = None
        self.status_lbl.configure(text="⬤ Déconnecté", text_color="#888")
        self.members_lbl.configure(text="👥 0 participant(s)")
        self.mic_status.configure(text="🎤 —", text_color="#888")
        self.join_btn.configure(state="normal")
        self.leave_btn.configure(state="disabled")
        self.mute_btn.configure(state="disabled", text="🎤 Mute",
                                fg_color="#444")

    # ─── Capture vidéo locale ───────────────────────────────────────────────
    def _start_capture(self):
        if self.cam_source is None:
            return
        self.cap = cv2.VideoCapture(self.cam_source)
        if not self.cap.isOpened():
            self.cam_status.configure(text="📷 Erreur", text_color="#e07050")
            return
        self.call_active = True
        self.cam_status.configure(text="📷 En cours ✅", text_color="#4caf50")
        self._send_frame()

    def _stop_capture(self):
        if self.frame_job:
            self.after_cancel(self.frame_job)
            self.frame_job = None
        if self.cap:
            self.cap.release()
            self.cap = None

    def _stop_audio(self):
        if self.audio_engine:
            self.audio_engine.stop()
            self.audio_engine = None

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

    # ─── Affichage vidéo distante ───────────────────────────────────────────
    def _show_remote_frame(self, name, b64_frame):
        try:
            buf   = base64.b64decode(b64_frame)
            arr   = np.frombuffer(buf, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except Exception as e:
            print(f"[Video] Erreur décodage frame de {name} : {e}")
            return

        if name not in self.tiles:
            self._add_tile(name)
        tile = self.tiles.get(name)
        if tile and tile.winfo_exists():
            tile.update_frame(img)

    # ─── Membres + Chat ─────────────────────────────────────────────────────
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
