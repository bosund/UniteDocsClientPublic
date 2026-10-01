"""
Unite Docs - Version Information
"""

# ENESTE kilde til versionsnummeret. Compileren (compiler/kompilerv3.py) laeser
# denne linje via regex, og app/__init__.py + unitedocs.py importerer den — så
# tallet vedligeholdes kun ét sted. __version_info__ udledes af strengen.
__version__ = "9.4.0"
__version_info__ = tuple(int(p) for p in __version__.split(".") if p.isdigit())

# Release notes for this version
RELEASE_NOTES = """
Version 9.4.0
- Opdateringstjekket sender et anonymt installations-id, så vi kan se, hvor
  mange der bruger programmet.

Version 9.3.1 - Sikkerhedsopdatering
- Sikkerhedsopdateringer af de indbyggede komponenter til billeder og
  dokumenter (Pillow, lxml).
- Opdateret Qt.
- RETTET: Fortryd-historikken (Ctrl+H) stod på dansk uanset sprogvalg.
"""
