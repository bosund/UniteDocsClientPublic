#!/usr/bin/env python3
"""
Script til at kompilere .po filer til .mo filer for alle sprog
"""
import os
import sys
from pathlib import Path

# Konsollen paa Windows er cp1252; scriptets statusmarkoerer er unicode. Uden
# dette doer koerslen paa en UnicodeEncodeError -- og skjuler den RIGTIGE fejl,
# fordi netop fejlgrenen er den der printer et unicode-kryds.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
try:
    from babel.messages import pofile, mofile
    babel_available = True
except ImportError:
    babel_available = False
    import gettext

def compile_po_file(po_path, mo_path):
    """Kompilerer en .po fil til en .mo fil"""
    try:
        if babel_available:
            # Brug Babel hvis tilgængelig
            with open(po_path, 'rb') as po_file:
                catalog = pofile.read_po(po_file)
            with open(mo_path, 'wb') as mo_file:
                mofile.write_mo(mo_file, catalog)
        else:
            # Brug gettext som fallback
            os.system(f'msgfmt "{po_path}" -o "{mo_path}"')
        print(f"✓ Kompilerede: {po_path} -> {mo_path}")
        return True
    except Exception as e:
        print(f"✗ Fejl ved kompilering af {po_path}: {e}")
        return False

def main():
    """Kompilerer alle .po filer i locales-mappen"""
    locales_dir = Path("app/locales")
    if not locales_dir.exists():
        print(f"Fejl: {locales_dir} findes ikke!")
        return
    
    success_count = 0
    total_count = 0
    
    for lang_dir in locales_dir.iterdir():
        if lang_dir.is_dir() and lang_dir.name != "__pycache__":
            po_file = lang_dir / "LC_MESSAGES" / "messages.po"
            mo_file = lang_dir / "LC_MESSAGES" / "messages.mo"
            
            if po_file.exists():
                total_count += 1
                if compile_po_file(po_file, mo_file):
                    success_count += 1
    
    print(f"\nKompilering fuldført: {success_count}/{total_count} filer kompileret succesfuldt.")

if __name__ == "__main__":
    main()
