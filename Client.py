
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

warnings.filterwarnings("ignore")
os.environ["PYTHONWARNINGS"] = "ignore"

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# ─── Paramètres à adapter ───────────────────────
DROIDCAM_IP   = "192.168.0.21"   # IP affichée sur l'app DroidCam
DROIDCAM_PORT = 4747
SERVER_IP     = "192.168.0.11"   # IP du PC hôte
SERVER_PORT   = 9765
# ────────────────────────────────────────────────

FRAME_QUALITY = 50
FRAME_RESIZE  = (320, 240)


def find_camera_source():
    """Teste DroidCam puis webcam locale. Vérifie qu'on reçoit vraiment des frames."""
    # 1. DroidCam via HTTP MJPEG
    url = f"http://{DROIDCAM_IP}:{DROIDCAM_PORT}/mjpegfeed"
    cap = cv2.VideoCapture(url)
    if cap.isOpened():
        ret, frame = cap.read()
        cap.release()
        if ret and frame is not None:
            print(f"[Camera] DroidCam trouvé : {url}")
            return url, "📱 DroidCam"

    # 2. Webcam locale index 0, 1, 2
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


# ──────────────────────────────────────────────────────
#  Tuile vidéo — tk.Frame natif (compatible place())
# ──────────────────────────────────────────────────────
class VideoTile(tk.Frame):
    def __init__(self, parent, name, is_me=False, **kwargs):
        bg = "#0d1117"
        super().__init__(parent, bg=bg, **kwargs)
        self.name  = name
        self.is_me = is_me

        bar_bg = "#1a4a8a" if is_me else "#2a2a4a"
        tag    = " (Vous)" if is_me else ""
        self.name_bar = tk.Label(
            self, text=f"  {name}{tag}",
            bg=bar_bg, fg="white",
            font=("Arial", 10, "bold"),
            anchor="w", height=1)
        self.name_bar.pack(side="bottom", fill="x")

        self.img_label = tk.Label(
            self, text="⏳\nEn attente…",
            bg=bg, fg="#555", font=("Arial", 16))
        self.img_label.pack(expand=True, fill="both")

    def update_frame(self, pil_image):
        try:
            w = max(self.winfo_width()  - 2, 120)
            h = max(self.winfo_height() - 26, 80)
            img   = pil_image.resize((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            self.img_label.configure(image=photo, text="")
            self.img_label.image = photo
        except Exception as e:
            print(f"[Tile] Erreur affichage : {e}")


# ──────────────────────────────────────────────────────
#  Application principale
# ──────────────────────────────────────────────────────
class VideoCallApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("📹 Salle Vidéo Partagée")
        self.geometry("1200x750")
        self.minsize(800, 550)
        self.resizable(True, True)

        self.ws           = None
        self.loop         = None
        self.net_thread   = None
        self.call_active  = False
        self.cap          = None
        self.cam_source   = None
        self.frame_job    = None
        self.my_name      = "Moi"

        try:
            self.audio = pyaudio.PyAudio()
        except Exception:
            self.audio = None
        self.audio_stream  = None
        self.audio_running = False

        self.tiles   = {}
        self.my_tile = None

        self._build_ui()
        self.bind("<Configure>", lambda e: self.after(100, self._relayout_grid))

    # ─────────────────────────────────────────────
    #  Interface
    # ─────────────────────────────────────────────
    def _build_ui(self):
        topbar = ctk.CTkFrame(self, height=52, fg_color="#161b22", corner_radius=0)
        topbar.pack(fill="x", side="top")
        topbar.pack_propagate(False)

        ctk.CTkLabel(topbar, text="🎥 Salle Vidéo Partagée",
                     font=("Arial", 18, "bold")).pack(side="left", padx=18)

        self.status_lbl = ctk.CTkLabel(
            topbar, text="⬤ Déconnecté",
            font=("Arial", 12), text_color="#888")
        self.status_lbl.pack(side="right", padx=18)

        self.members_lbl = ctk.CTkLabel(
            topbar, text="👥 0 participant(s)",
            font=("Arial", 11), text_color="#aaa")
        self.members_lbl.pack(side="right", padx=10)

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

        ctk.CTkLabel(form, text="IP serveur :", width=70,
                     font=("Arial", 11)).pack(side="left", padx=(0, 2))
        self.server_entry = ctk.CTkEntry(
            form, placeholder_text=SERVER_IP, width=130, height=32)
        self.server_entry.pack(side="left", padx=(0, 10))

        self.cam_status = ctk.CTkLabel(
            form, text="📷 —", font=("Arial", 10), text_color="#888")
        self.cam_status.pack(side="left", padx=(0, 10))

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
        self.leave_btn.pack(side="left")

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

        self.chat_box = ctk.CTkTextbox(chat_panel, font=("Arial", 11), wrap="word")
        self.chat_box.pack(fill="both", expand=True, padx=8, pady=(0, 6))
        self.chat_box.configure(state="disabled")

        chat_row = ctk.CTkFrame(chat_panel, fg_color="transparent")
        chat_row.pack(fill="x", padx=8, pady=(0, 10))
        self.chat_entry = ctk.CTkEntry(
            chat_row, placeholder_text="Message…", height=32)
        self.chat_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.chat_entry.bind("<Return>", lambda e: self._send_chat())
        ctk.CTkButton(chat_row, text="↩", width=34, height=32,
                      command=self._send_chat).pack(side="left")

    # ─────────────────────────────────────────────
    #  Grille dynamique
    # ─────────────────────────────────────────────
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

    # ─────────────────────────────────────────────
    #  Connexion
    # ─────────────────────────────────────────────
    def _join(self):
        name   = self.name_entry.get().strip() or "Anonyme"
        room   = self.room_entry.get().strip()  or "general"
        srv_ip = self.server_entry.get().strip() or SERVER_IP
        uri    = f"ws://{srv_ip}:{SERVER_PORT}"

        self.my_name = name
        self.cam_status.configure(text="📷 Recherche…", text_color="#aaa")
        self.update()

        src, lbl = find_camera_source()
        if src is None:
            self.cam_status.configure(text="📷 Aucune caméra", text_color="#e07050")
            msgbox.showwarning("Caméra",
                               "Aucune caméra détectée.\n"
                               "Vérifiez que DroidCam est lancé\n"
                               "ou qu'une webcam est branchée.")
        else:
            self.cam_status.configure(text=f"📷 {lbl}", text_color="#7ec88a")

        self.cam_source = src
        self.my_tile = self._add_tile(name, is_me=True)

        self.loop = asyncio.new_event_loop()
        self.net_thread = threading.Thread(
            target=lambda: (
                asyncio.set_event_loop(self.loop),
                self.loop.run_until_complete(self._connect(uri, name, room))
            ), daemon=True)
        self.net_thread.start()
        self.join_btn.configure(state="disabled")

    async def _connect(self, uri, name, room):
        try:
            async with websockets.connect(uri, max_size=10_000_000) as ws:
                self.ws = ws
                await ws.send(json.dumps(
                    {"type": "join", "name": name, "room": room}))
                # Démarrer la caméra dès la connexion établie
                self.after(0, self._start_capture)
                async for raw in ws:
                    self._on_message(raw)
        except Exception as e:
            self.after(0, lambda: self._on_disconnect(str(e)))

    def _on_message(self, raw):
        try:
            data = json.loads(raw)
        except Exception:
            return
        t = data.get("type")

        if t == "joined":
            self.after(0, lambda: self._on_joined(data))
        elif t == "user_joined":
            n = data["name"]
            self.after(0, lambda: self._add_tile(n))
            self.after(0, lambda: self._add_chat_line(f"✅ {n} a rejoint"))
        elif t == "user_left":
            n = data["name"]
            self.after(0, lambda: self._remove_tile(n))
            self.after(0, lambda: self._add_chat_line(f"👋 {n} a quitté"))
        elif t == "members":
            self.after(0, lambda: self._update_members(data["members"]))
        elif t == "video":
            name  = data["name"]
            frame = data["frame"]
            self.after(0, lambda: self._show_remote_frame(name, frame))
        elif t == "chat":
            self.after(0, lambda: self._add_chat_line(
                f"[{data['time']}] {data['name']} : {data['text']}"))

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
        self._stop_capture()
        if reason:
            self._add_chat_line(f"⚠ Déconnecté : {reason}")

    def _leave(self):
        self.call_active = False
        self._stop_capture()
        if self.ws and self.loop:
            asyncio.run_coroutine_threadsafe(self.ws.close(), self.loop)
        for tile in list(self.tiles.values()):
            tile.destroy()
        self.tiles.clear()
        self.my_tile = None
        self.status_lbl.configure(text="⬤ Déconnecté", text_color="#888")
        self.members_lbl.configure(text="👥 0 participant(s)")
        self.join_btn.configure(state="normal")
        self.leave_btn.configure(state="disabled")

    # ─────────────────────────────────────────────
    #  Capture vidéo  ← CORRECTION PRINCIPALE
    # ─────────────────────────────────────────────
    def _start_capture(self):
        if self.cam_source is None:
            print("[Camera] Pas de source disponible")
            return

        print(f"[Camera] Ouverture : {self.cam_source}")
        self.cap = cv2.VideoCapture(self.cam_source)

        if not self.cap.isOpened():
            print("[Camera] ❌ Impossible d'ouvrir la caméra")
            self.cam_status.configure(text="📷 Erreur", text_color="#e07050")
            return

        # ← FIX CRITIQUE : call_active = True ICI (pas dans _on_joined)
        self.call_active = True
        print("[Camera] ✅ Capture démarrée")
        self.cam_status.configure(text="📷 En cours ✅", text_color="#4caf50")
        self._send_frame()

    def _stop_capture(self):
        if self.frame_job:
            self.after_cancel(self.frame_job)
            self.frame_job = None
        if self.cap:
            self.cap.release()
            self.cap = None

    def _send_frame(self):
        if not self.call_active or self.cap is None:
            return

        ret, frame = self.cap.read()
        if ret and frame is not None:
            # Ma propre vidéo dans ma tuile
            if self.my_tile and self.my_tile.winfo_exists():
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                self.my_tile.update_frame(Image.fromarray(rgb))

            # Encoder et envoyer
            small = cv2.resize(frame, FRAME_RESIZE)
            ok, buf = cv2.imencode(
                ".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, FRAME_QUALITY])
            if ok and self.ws and self.loop and not self.loop.is_closed():
                b64 = base64.b64encode(buf.tobytes()).decode()
                asyncio.run_coroutine_threadsafe(
                    self.ws.send(json.dumps({"type": "video", "frame": b64})),
                    self.loop)

        self.frame_job = self.after(50, self._send_frame)

    # ─────────────────────────────────────────────
    #  Affichage vidéo distante
    # ─────────────────────────────────────────────
    def _show_remote_frame(self, name, b64_frame):
        try:
            buf   = base64.b64decode(b64_frame)
            arr   = np.frombuffer(buf, dtype=np.uint8)
            frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if frame is None:
                return
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        except Exception as e:
            print(f"[Video] Erreur décodage : {e}")
            return

        if name not in self.tiles:
            self._add_tile(name)
        self.tiles[name].update_frame(img)

    # ─────────────────────────────────────────────
    #  Membres + Chat
    # ─────────────────────────────────────────────
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
        if self.audio:
            try:
                self.audio.terminate()
            except Exception:
                pass
        self.destroy()


if __name__ == "__main__":
    app = VideoCallApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()