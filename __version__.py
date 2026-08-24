"""
Unite Docs - Version Information
"""

# ENESTE kilde til versionsnummeret. Compileren (compiler/kompilerv3.py) laeser
# denne linje via regex, og app/__init__.py + unitedocs.py importerer den — så
# tallet vedligeholdes kun ét sted. __version_info__ udledes af strengen.
__version__ = "8.0.0"
__version_info__ = tuple(int(p) for p in __version__.split(".") if p.isdigit())

# Release notes for this version
RELEASE_NOTES = """
Version 8.0.0 - PyMuPDF-motor og eksport til Markdown/ePub/tekst
- Ny PDF-motor: hele appen kører nu på PyMuPDF (fitz). pikepdf, pypdfium2 og
  reportlab er udgået.
- Nyt: Flet og gem samt Gem enkeltfiler kan nu eksportere til PDF, Markdown
  (.md), ePub (.epub) eller ren tekst (.txt). Vælg format i dialogen.
- Sider uden tekstlag (typisk scanninger) markeres tydeligt i teksteksporten,
  så intet forsvinder tavst.
- Ny Credits-oversigt i bundlinjen med licenser for alle anvendte biblioteker.
- Licens: Unite Docs distribueres nu under AGPL-3.0.
"""
