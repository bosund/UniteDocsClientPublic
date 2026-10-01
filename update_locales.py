#!/usr/bin/env python3
"""
Script til at opdatere oversættelsesfilerne med nye tekster
"""
import os
import subprocess
import sys
from pathlib import Path

# Konsollen paa Windows er cp1252, og scriptets statuslinjer bruger "OK"-hak og
# danske tegn. Uden dette doer koerslen paa en UnicodeEncodeError efter at
# arbejdet i virkeligheden er lykkedes.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

def extract_messages():
    """Udtrækker alle oversættelige beskeder fra Python-filerne"""
    print("Udtrækker beskeder fra kildekoden...")
    
    # Kør pybabel extract for at opdatere .pot filen
    result = subprocess.run([
        'pybabel', 'extract', 
        '-F', 'babel.cfg',
        '-k', '_',
        # Undo-etiketter: gemmes uoversat og oversaettes ved visning (undo_stack.N_).
        '-k', 'N_',
        '-o', 'app/locales/messages.pot',
        'app/'
    ], capture_output=True, text=True)
    
    if result.returncode != 0:
        print(f"Fejl ved udtrækning af beskeder: {result.stderr}")
        return False
        
    print("✓ Beskeder udtrukket til messages.pot")
    return True

def update_po_files():
    """Opdaterer alle .po filer med nye beskeder fra .pot filen"""
    print("Opdaterer .po filer...")
    
    locales_dir = Path("app/locales")
    pot_file = locales_dir / "messages.pot"
    
    success_count = 0
    total_count = 0
    
    for lang_dir in locales_dir.iterdir():
        if lang_dir.is_dir() and lang_dir.name != "__pycache__":
            po_file = lang_dir / "LC_MESSAGES" / "messages.po"
            
            if po_file.exists():
                total_count += 1
                
                # --no-fuzzy-matching er IKKE valgfri.
                #
                # Uden flaget gætter pybabel en oversættelse til hver ny streng
                # ved at kopiere den mest STRENGLIGNENDE eksisterende og sætte
                # "#, fuzzy" på. Målt på denne kodebase gav det 38 gæt pr.
                # sprog, og ikke ét af dem var rigtigt:
                #
                #     "Kopiér tekst"  -> "Enter text:"
                #     "Valgte sider"  -> "Delete page"
                #     "Oprettet: %s"  -> "Downloaded %s"
                #
                # Babel kompilerer ikke fuzzy-poster ind i .mo, så gættet viser
                # sig som DANSK tekst midt i en engelsk flade — tavst nok til at
                # nå en udgivelse. Og fjerner nogen flaget uden at læse teksten,
                # bliver gættet til en rigtig oversættelse.
                #
                # Med flaget kommer en ny streng ud som TOM. Det er en tilstand
                # check_locales.py kan se, og som ikke ligner en oversættelse.
                result = subprocess.run([
                    'pybabel', 'update',
                    '-i', str(pot_file),
                    '-d', 'app/locales',
                    '-l', lang_dir.name,
                    '--no-fuzzy-matching'
                ], capture_output=True, text=True)
                
                if result.returncode == 0:
                    print(f"✓ Opdaterede: {lang_dir.name}")
                    success_count += 1
                else:
                    print(f"✗ Fejl ved opdatering af {lang_dir.name}: {result.stderr}")
    
    print(f"\nOpdatering fuldført: {success_count}/{total_count} sprog opdateret.")
    return success_count == total_count

def fill_source_language():
    """Dansk msgstr = msgid.

    Kildeteksten ER dansk, så det danske katalog er identitet. Gøres det ikke
    her, betyder "tom msgstr" to forskellige ting alt efter sprog — på dansk
    "fint, falder tilbage på kilden", på engelsk "mangler" — og så kan
    check_locales.py ikke stille det samme krav til alle otte."""
    try:
        from check_locales import fill_danish
    except ImportError as e:
        print(f"Kunne ikke udfylde det danske katalog: {e}")
        return
    n = fill_danish()
    print(f"✓ Dansk: {n} poster sat til identitet"
          if n else "✓ Dansk: allerede komplet")


def main():
    """Hovedfunktion"""
    print("=== Opdatering af oversættelsesfilerne ===\n")

    if not extract_messages():
        return

    if not update_po_files():
        return

    fill_source_language()

    print("\n=== Næste trin ===")
    print("1. python client/check_locales.py --list manglende.json")
    print("2. Udfyld hvert felt i manglende.json (alle 7 fremmedsprog)")
    print("3. python client/check_locales.py --apply manglende.json")
    print("4. python client/compile_locales.py")
    print("\nTrin 1-3 er en opgave en AI-assistent kan løse i ét hug.")
    print("check_locales.py uden argumenter er gaten: den fejler hvis noget mangler.")

if __name__ == "__main__":
    main()
