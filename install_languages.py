"""
install_languages.py — Installation de TOUTES les paires de traduction
───────────────────────────────────────────────────────────────────────
9 langues × 8 directions = 72 paires
Langues : fr, en, ar, es, de, zh, pt, it, mg (Malagasy)

Usage :
    python install_languages.py
"""

import argostranslate.package as pkg
import argostranslate.translate

LANGUAGES = ["en", "fr", "ar", "es", "de", "zh", "pt", "it", "mg"]

LANGUAGE_NAMES = {
    "en": "Anglais",
    "fr": "Français",
    "ar": "Arabe",
    "es": "Espagnol",
    "de": "Allemand",
    "zh": "Chinois",
    "pt": "Portugais",
    "it": "Italien",
    "mg": "Malagasy",
}

def build_all_pairs():
    """Génère toutes les paires src → tgt (72 paires)."""
    return [
        (src, tgt)
        for src in LANGUAGES
        for tgt in LANGUAGES
        if src != tgt
    ]

def get_already_installed() -> set:
    """Retourne l'ensemble des paires déjà installées."""
    installed_set = set()
    try:
        installed_langs = argostranslate.translate.get_installed_languages()
        for src_lang in installed_langs:
            for tgt_lang in installed_langs:
                if src_lang.code != tgt_lang.code:
                    if src_lang.get_translation(tgt_lang):
                        installed_set.add((src_lang.code, tgt_lang.code))
    except Exception as e:
        print(f"  ⚠️  Erreur vérification packs installés : {e}")
    return installed_set

def install_all():
    print("=" * 55)
    print("  PolyMeet — Installation des packs de traduction")
    print("  9 langues × 8 directions = 72 paires")
    print("=" * 55)

    print("\n🔄 Mise à jour de l'index des paquets…")
    try:
        pkg.update_package_index()
        print("  ✅ Index mis à jour.\n")
    except Exception as e:
        print(f"  ⚠️  Impossible de mettre à jour l'index : {e}")
        print("  → Vérifiez votre connexion internet.\n")

    available  = pkg.get_available_packages()
    pairs      = build_all_pairs()
    already    = get_already_installed()

    print(f"📋 {len(pairs)} paires à vérifier\n")

    success, skipped, failed, unavailable = 0, 0, 0, 0

    for src, tgt in pairs:
        src_name = LANGUAGE_NAMES[src]
        tgt_name = LANGUAGE_NAMES[tgt]

        # Déjà installé ?
        if (src, tgt) in already:
            print(f"  ✔️  {src_name} → {tgt_name} : déjà installé")
            skipped += 1
            continue

        # Chercher dans l'index
        match = next(
            (p for p in available if p.from_code == src and p.to_code == tgt),
            None
        )

        if match is None:
            print(f"  ⚠️  {src_name} → {tgt_name} : absent de l'index argostranslate")
            unavailable += 1
            continue

        print(f"  📦 Installation {src_name} → {tgt_name}…", end=" ", flush=True)
        try:
            pkg.install_from_path(match.download())
            print("✅")
            success += 1
        except Exception as e:
            print(f"❌ ({e})")
            failed += 1

    # ── Résumé ──────────────────────────────────────────────────────────────
    print(f"\n{'═' * 55}")
    print(f"  ✅ Installés      : {success}")
    print(f"  ✔️  Déjà présents  : {skipped}")
    print(f"  ⚠️  Non disponibles : {unavailable}")
    print(f"  ❌ Échecs          : {failed}")
    print(f"{'═' * 55}")

    if unavailable > 0:
        print("""
ℹ️  Note sur les paires non disponibles :
   Certaines paires (souvent impliquant 'mg' Malagasy)
   peuvent être absentes de l'index argostranslate.
   
   → PolyMeet utilisera automatiquement un PIVOT via
     l'anglais ou le français pour ces paires.
   
   Exemple : Malagasy → Arabe passera par :
             Malagasy → Anglais → Arabe
""")

    print("\n🎉 Installation terminée ! Vous pouvez lancer PolyMeet.")

if __name__ == "__main__":
    install_all()