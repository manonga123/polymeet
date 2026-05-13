# main.py — Point d'entrée PolyMeet
from Client import VideoCallApp

if __name__ == "__main__":
    app = VideoCallApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()