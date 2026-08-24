"""Enkelt kilde for de anvendte biblioteker og deres licenser.

Både Credits-vinduet i appen og en genereret NOTICE-fil bør bygges herfra, så
de aldrig kommer ud af sync. Registrér enhver ny afhængighed her (jf. CLAUDE.md
regel 4).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Library:
    name: str
    version: str
    license: str
    url: str


# Rækkefølge: motor/eksport først, derefter UI og runtime.
CREDITS = (
    Library("PyMuPDF", "1.28.2", "AGPL-3.0", "https://pymupdf.readthedocs.io/"),
    Library("pymupdf4llm", "0.3.4", "AGPL-3.0", "https://pymupdf.readthedocs.io/en/latest/pymupdf4llm/"),
    Library("EbookLib", "0.20", "AGPL-3.0", "https://github.com/aerkalov/ebooklib"),
    Library("lxml", "6.0.2", "BSD-3-Clause", "https://lxml.de/"),
    Library("Markdown", "3.10.3", "BSD-3-Clause", "https://python-markdown.github.io/"),
    Library("tabulate", "0.10.0", "MIT", "https://github.com/astanin/python-tabulate"),
    Library("Pillow", "12.2.0", "MIT-CMU", "https://python-pillow.org/"),
    Library("Tesseract OCR (tessdata)", "", "Apache-2.0", "https://github.com/tesseract-ocr/tessdata_fast"),
    Library("pycryptodome", "3.23.0", "BSD-2-Clause / Public Domain", "https://www.pycryptodome.org/"),
    Library("sv-ttk", "2.6.1", "MIT", "https://github.com/rdbende/Sun-Valley-ttk-theme"),
    Library("tkinterdnd2", "0.4.3", "MIT", "https://github.com/pmgagne/tkinterdnd2"),
    Library("Python / Tcl-Tk", "", "PSF / BSD-lignende", "https://www.python.org/"),
)

# Det offentlige kildekode-repo. AGPL-3.0 §6 kræver at modtagere af binæren kan
# få den tilsvarende kildekode; dette link er det tilbud.
SOURCE_URL = "https://github.com/bosund/UniteDocsClientPublic"


def notice_text() -> str:
    """Ren tekst til en NOTICE-fil."""
    lines = [
        "Unite Docs",
        "Licens: AGPL-3.0",
        "Kildekode: " + SOURCE_URL,
        "",
        "Anvendte biblioteker:",
    ]
    for lib in CREDITS:
        ver = (" " + lib.version) if lib.version else ""
        lines.append(f"  - {lib.name}{ver} — {lib.license} — {lib.url}")
    return "\n".join(lines) + "\n"
