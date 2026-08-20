#!/usr/bin/env python3
"""
Script til at opdatere oversættelsesfilerne med nye tekster
"""
import os
import subprocess
from pathlib import Path

def extract_messages():
    """Udtrækker alle oversættelige beskeder fra Python-filerne"""
    print("Udtrækker beskeder fra kildekoden...")
    
    # Kør pybabel extract for at opdatere .pot filen
    result = subprocess.run([
        'pybabel', 'extract', 
        '-F', 'babel.cfg',
        '-k', '_',
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
                
                # Kør pybabel update
                result = subprocess.run([
                    'pybabel', 'update',
                    '-i', str(pot_file),
                    '-d', 'app/locales',
                    '-l', lang_dir.name
                ], capture_output=True, text=True)
                
                if result.returncode == 0:
                    print(f"✓ Opdaterede: {lang_dir.name}")
                    success_count += 1
                else:
                    print(f"✗ Fejl ved opdatering af {lang_dir.name}: {result.stderr}")
    
    print(f"\nOpdatering fuldført: {success_count}/{total_count} sprog opdateret.")
    return success_count == total_count

def main():
    """Hovedfunktion"""
    print("=== Opdatering af oversættelsesfilerne ===\n")
    
    if not extract_messages():
        return
    
    if not update_po_files():
        return
    
    print("\n=== Næste trin ===")
    print("1. Rediger .po filerne og tilføj manglende oversættelser")
    print("2. Kør 'python compile_locales.py' for at kompilere ændringerne")

if __name__ == "__main__":
    main()
