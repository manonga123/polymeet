import customtkinter as ctk
import cv2
import threading
from PIL import Image
import pyaudio
import tkinter.messagebox as msgbox

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

DROIDCAM_IP   = "192.168.0.45"   # ← IP affichée sur l'app DroidCam
DROIDCAM_PORT = 4747

# ──────────────────────────────────────────────
#  Sources caméra disponibles
# ──────────────────────────────────────────────
def build_camera_sources():
    """
    Retourne une liste de tuples (label, source) où source est
    soit un int (index V4L2) soit une str (URL HTTP DroidCam).
    """
    sources = []

    # 1. Flux HTTP DroidCam (prioritaire, contourne les conflits /dev/video0)
    droidcam_url = f"http://{DROIDCAM_IP}:{DROIDCAM_PORT}/mjpegfeed"
    cap = cv2.VideoCapture(droidcam_url)
    if cap.isOpened():
        sources.append(("📱 DroidCam (WiFi)", droidcam_url))
        cap.release()

    # 2. Caméras V4L2 locales
    for i in range(5):
        cap = cv2.VideoCapture(i)
        if cap.isOpened():
            label = "💻 Webcam PC" if i == 0 else f"Caméra locale (index {i})"
            sources.append((label, i))
            cap.release()

    return sources


# ──────────────────────────────────────────────
#  Application principale
# ──────────────────────────────────────────────
class VideoCallApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("📹 Appel Vidéo – DroidCam / Webcam")
        self.geometry("900x660")
        self.resizable(True, True)

        self.cap          = None
        self.call_active  = False
        self.source       = None   # int ou str
        self.frame_job    = None
        self.all_sources  = []
        self.src_ptr      = 0

        self.audio         = pyaudio.PyAudio()
        self.audio_stream  = None
        self.audio_thread  = None
        self.audio_running = False

        self._build_ui()
        self._detect_sources()

    # ──────────────────────────────────────────
    #  UI
    # ──────────────────────────────────────────
    def _build_ui(self):
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(pady=(18, 0), padx=24, fill="x")

        self.title_label = ctk.CTkLabel(
            header, text="Bienvenue", font=("Arial", 26, "bold"))
        self.title_label.pack(side="left")

        self.status_dot = ctk.CTkLabel(
            header, text="⬤ Hors-ligne", font=("Arial", 13), text_color="#888")
        self.status_dot.pack(side="right", padx=8)

        name_frame = ctk.CTkFrame(self, fg_color="transparent")
        name_frame.pack(pady=8, padx=24, fill="x")

        self.entry = ctk.CTkEntry(
            name_frame, placeholder_text="Entrez votre nom", width=260)
        self.entry.pack(side="left", padx=(0, 10))

        ctk.CTkButton(
            name_frame, text="Valider", width=100,
            command=self._show_name).pack(side="left")

        # Zone vidéo
        self.video_frame = ctk.CTkFrame(self, corner_radius=12)
        self.video_frame.pack(pady=10, padx=24, fill="both", expand=True)

        self.video_label = ctk.CTkLabel(
            self.video_frame,
            text="📷  Aucun flux vidéo\nAppuyez sur « Démarrer l'appel »",
            font=("Arial", 16), text_color="#aaa")
        self.video_label.pack(expand=True, fill="both", padx=4, pady=4)

        # Barre caméra
        cam_bar = ctk.CTkFrame(self, fg_color="transparent")
        cam_bar.pack(pady=(0, 4), padx=24, fill="x")

        self.cam_label = ctk.CTkLabel(
            cam_bar, text="Caméra : détection…",
            font=("Arial", 12), text_color="#aaa")
        self.cam_label.pack(side="left")

        self.cam_switch_btn = ctk.CTkButton(
            cam_bar, text="🔄 Changer de caméra",
            width=160, height=28, font=("Arial", 12),
            command=self._switch_camera, state="disabled")
        self.cam_switch_btn.pack(side="right")

        # Boutons
        btn_bar = ctk.CTkFrame(self, fg_color="transparent")
        btn_bar.pack(pady=(0, 18), padx=24, fill="x")

        self.start_btn = ctk.CTkButton(
            btn_bar, text="📞 Démarrer l'appel",
            fg_color="#1a7a3c", hover_color="#145e2e",
            font=("Arial", 15, "bold"), height=42,
            command=self._start_call)
        self.start_btn.pack(side="left", expand=True, fill="x", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            btn_bar, text="📵 Terminer",
            fg_color="#b03030", hover_color="#8a2020",
            font=("Arial", 15, "bold"), height=42,
            command=self._stop_call, state="disabled")
        self.stop_btn.pack(side="left", expand=True, fill="x")

    # ──────────────────────────────────────────
    #  Détection des sources
    # ──────────────────────────────────────────
    def _detect_sources(self):
        def task():
            sources = build_camera_sources()
            self.after(0, lambda: self._on_sources_detected(sources))
        threading.Thread(target=task, daemon=True).start()

    def _on_sources_detected(self, sources):
        if not sources:
            self.cam_label.configure(
                text="⚠ Aucune caméra détectée", text_color="#e07050")
            msgbox.showwarning(
                "Aucune caméra",
                f"Aucune caméra trouvée.\n\n"
                f"Pour DroidCam :\n"
                f"1. Ouvrez l'app DroidCam sur votre téléphone\n"
                f"2. Vérifiez que l'IP est bien {DROIDCAM_IP}\n"
                f"3. Lancez : droidcam-cli {DROIDCAM_IP} {DROIDCAM_PORT}\n"
                f"4. Relancez ce programme")
            return

        self.all_sources = sources
        self.src_ptr = 0
        label, self.source = sources[0]
        self.cam_label.configure(
            text=f"Source : {label}", text_color="#7ec88a")

        if len(sources) > 1:
            self.cam_switch_btn.configure(state="normal")

    # ──────────────────────────────────────────
    #  Changer de source
    # ──────────────────────────────────────────
    def _switch_camera(self):
        if not self.all_sources:
            return
        was_active = self.call_active
        if was_active:
            self._stop_call()

        self.src_ptr = (self.src_ptr + 1) % len(self.all_sources)
        label, self.source = self.all_sources[self.src_ptr]
        self.cam_label.configure(text=f"Source : {label}", text_color="#7ec88a")

        if was_active:
            self._start_call()

    def _show_name(self):
        name = self.entry.get().strip()
        if name:
            self.title_label.configure(text=f"Bonjour {name} 👋")

    # ──────────────────────────────────────────
    #  Démarrer / Arrêter l'appel
    # ──────────────────────────────────────────
    def _start_call(self):
        if self.source is None:
            msgbox.showerror("Erreur", "Aucune source caméra disponible.")
            return

        self.cap = cv2.VideoCapture(self.source)
        if not self.cap.isOpened():
            src_str = self.source if isinstance(self.source, str) else f"index {self.source}"
            msgbox.showerror(
                "Caméra inaccessible",
                f"Impossible d'ouvrir : {src_str}\n\n"
                f"Pour DroidCam WiFi :\n"
                f"• Vérifiez que l'app tourne sur le téléphone\n"
                f"• Vérifiez que droidcam-cli est lancé\n"
                f"• IP actuelle : {DROIDCAM_IP}:{DROIDCAM_PORT}")
            return

        self.call_active = True
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_dot.configure(text="⬤ En appel", text_color="#4caf50")
        self._update_frame()
        self._start_audio()

    def _stop_call(self):
        self.call_active = False
        if self.frame_job:
            self.after_cancel(self.frame_job)
            self.frame_job = None
        if self.cap:
            self.cap.release()
            self.cap = None
        self._stop_audio()
        self.video_label.configure(
            image=None,
            text="📷  Appel terminé\nAppuyez sur « Démarrer l'appel »")
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_dot.configure(text="⬤ Hors-ligne", text_color="#888")

    # ──────────────────────────────────────────
    #  Boucle vidéo
    # ──────────────────────────────────────────
    def _update_frame(self):
        if not self.call_active or self.cap is None:
            return
        ret, frame = self.cap.read()
        if ret:
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            img   = Image.fromarray(frame)
            w = self.video_frame.winfo_width()  - 8
            h = self.video_frame.winfo_height() - 8
            if w > 10 and h > 10:
                img = img.resize((w, h), Image.LANCZOS)
            ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(w, h))
            self.video_label.configure(image=ctk_img, text="")
            self.video_label.image = ctk_img
        self.frame_job = self.after(30, self._update_frame)

    # ──────────────────────────────────────────
    #  Audio
    # ──────────────────────────────────────────
    def _start_audio(self):
        mic_index = self._find_droidcam_mic()
        try:
            self.audio_stream = self.audio.open(
                format=pyaudio.paInt16, channels=1, rate=44100,
                input=True, input_device_index=mic_index,
                frames_per_buffer=1024)
            self.audio_running = True
            self.audio_thread  = threading.Thread(
                target=self._audio_loop, daemon=True)
            self.audio_thread.start()
            src = f"DroidCam mic (index {mic_index})" if mic_index is not None \
                  else "micro système par défaut"
            print(f"[Audio] Capture démarrée — {src}")
        except Exception as e:
            print(f"[Audio] Impossible d'ouvrir le micro : {e}")

    def _find_droidcam_mic(self):
        for i in range(self.audio.get_device_count()):
            info = self.audio.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 \
               and "droidcam" in info.get("name", "").lower():
                print(f"[Audio] DroidCam mic : {info['name']} (index {i})")
                return i
        return None

    def _audio_loop(self):
        while self.audio_running and self.audio_stream:
            try:
                self.audio_stream.read(1024, exception_on_overflow=False)
            except Exception:
                break

    def _stop_audio(self):
        self.audio_running = False
        if self.audio_stream:
            try:
                self.audio_stream.stop_stream()
                self.audio_stream.close()
            except Exception:
                pass
            self.audio_stream = None

    def on_closing(self):
        self._stop_call()
        self.audio.terminate()
        self.destroy()


if __name__ == "__main__":
    app = VideoCallApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()