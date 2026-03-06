#test fotsiny
import customtkinter as ctk

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

app = ctk.CTk()
app.title("Dashboard")
app.geometry("600x400")

frame = ctk.CTkFrame(app)
frame.pack(pady=40, padx=40, fill="both", expand=True)

title = ctk.CTkLabel(frame, text="Bienvenue", font=("Arial", 24))
title.pack(pady=20)

entry = ctk.CTkEntry(frame, placeholder_text="Entrez votre nom")
entry.pack(pady=10)

def show_name():
    title.configure(text=f"Bonjour {entry.get()} 👋")

btn = ctk.CTkButton(frame, text="Valider", command=show_name)
btn.pack(pady=10)

app.mainloop()