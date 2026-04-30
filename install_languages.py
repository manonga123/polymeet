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