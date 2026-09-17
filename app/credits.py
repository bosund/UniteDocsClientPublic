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
    # --- Automatisk anonymisering -------------------------------------
    # Modellens vaegte er CC BY-SA 4.0 (share-alike). Den distribueres som en
    # DATAMAPPE ved siden af binaeren (models/da_core_news_md, med sin egen
    # LICENSE-fil), ikke linket ind i den, og attributionen staar her og i NOTICE.
    Library("Microsoft Presidio (presidio-analyzer)", "2.2.364", "MIT",
            "https://microsoft.github.io/presidio/"),
    Library("spaCy", "3.8.16", "MIT", "https://spacy.io/"),
    Library("da_core_news_md (dansk spaCy-model)", "3.8.0", "CC BY-SA 4.0",
            "https://github.com/explosion/spacy-models"),
    # Den engelske model er MIT og bruges paa sider hvor sprogdetektoren finder
    # engelsk. Ogsaa en datamappe ved siden af binaeren.
    Library("en_core_web_md (engelsk spaCy-model)", "3.8.0", "MIT",
            "https://github.com/explosion/spacy-models"),
    # Sprogdetektoren: afgoer hvilken af de to modeller en side skal igennem.
    # Kun predict-delen af fastText (ingen traening, ingen numpy). Selve
    # sprog-id-modellen lid.176.ftz er CC BY-SA 3.0 og ligger som datafil ved
    # siden af binaeren (models/lid.176.ftz), ikke linket ind i den.
    Library("fasttext-predict", "0.9.2.4", "MIT",
            "https://github.com/searxng/fasttext-predict"),
    Library("fastText lid.176 (sprogdetektion)", "", "CC BY-SA 3.0",
            "https://fasttext.cc/docs/en/language-identification.html"),
    # Postnummertabellen verificerer at et firecifret tal foran et bynavn
    # faktisk ER et postnummer med netop det bynavn. Danske adressedata er frie
    # offentlige data; tabellen er genereret ind i app/postnumre.py.
    Library("Danmarks Adresseregister (postnumre)", "", "Frie offentlige data",
            "https://dataforsyningen.dk/"),
    Library("thinc", "8.3.13", "MIT", "https://thinc.ai/"),
    Library("blis", "1.3.3", "BSD-3-Clause", "https://github.com/explosion/cython-blis"),
    Library("NumPy", "2.4.6", "BSD-3-Clause", "https://numpy.org/"),
    Library("srsly", "2.5.3", "MIT", "https://github.com/explosion/srsly"),
    Library("catalogue", "2.0.10", "MIT", "https://github.com/explosion/catalogue"),
    Library("cymem", "2.0.13", "MIT", "https://github.com/explosion/cymem"),
    Library("preshed", "3.0.13", "MIT", "https://github.com/explosion/preshed"),
    Library("murmurhash", "1.0.15", "MIT", "https://github.com/explosion/murmurhash"),
    Library("wasabi", "1.1.3", "MIT", "https://github.com/explosion/wasabi"),
    Library("confection", "1.3.3", "MIT", "https://github.com/explosion/confection"),
    Library("pydantic", "2.13.4", "MIT", "https://pydantic.dev/"),
    Library("regex", "2026.7.19", "Apache-2.0", "https://github.com/mrabarnett/mrab-regex"),
    Library("tldextract", "5.3.2", "BSD-3-Clause", "https://github.com/john-kurkowski/tldextract"),
    Library("phonenumbers", "9.0.37", "Apache-2.0", "https://github.com/daviddrysdale/python-phonenumbers"),
    Library("PyYAML", "6.0.3", "MIT", "https://pyyaml.org/"),
    Library("click", "8.4.2", "BSD-3-Clause", "https://click.palletsprojects.com/"),
    # --- Transitivt via spaCy/Presidio -------------------------------
    # Verificeret mod builden: hver af disse har en 'module.<navn>.c' i
    # unitedocs.build, dvs. de kompileres FAKTISK ind i exe'en. spacy-legacy,
    # spacy-loggers, smart_open og rich gor ikke og staar derfor ikke her.
    #
    # requests/urllib3/certifi/idna/charset-normalizer/requests-file er med
    # fordi 'spacy/__init__' -> 'weasel' -> 'import requests' er ubetinget.
    # Appen KALDER dem ikke; updater.py er fortsat eneste netvaerkssti
    # (CLAUDE.md regel 8). Alle licenser er forenelige med AGPL-3.0 —
    # PSF-2.0 (typing-extensions) staar ikke eksplicit i regel 4's liste,
    # men er permissiv og uden vilkaar der strider mod videredistribution.
    Library("requests", "2.34.2", "Apache-2.0", "https://requests.readthedocs.io/"),
    Library("urllib3", "2.7.0", "MIT", "https://urllib3.readthedocs.io/"),
    Library("certifi", "2026.7.22", "MPL-2.0", "https://github.com/certifi/python-certifi"),
    Library("idna", "3.19", "BSD-3-Clause", "https://github.com/kjd/idna"),
    Library("charset-normalizer", "3.4.7", "MIT", "https://github.com/jawah/charset_normalizer"),
    Library("requests-file", "3.0.1", "Apache-2.0", "https://github.com/dashea/requests-file"),
    Library("weasel", "1.0.0", "MIT", "https://github.com/explosion/weasel"),
    Library("Jinja2", "3.1.6", "BSD-3-Clause", "https://jinja.palletsprojects.com/"),
    Library("MarkupSafe", "3.0.3", "BSD-3-Clause", "https://github.com/pallets/markupsafe"),
    Library("typer", "0.27.1", "MIT", "https://typer.tiangolo.com/"),
    Library("shellingham", "1.5.4", "ISC", "https://github.com/sarugaku/shellingham"),
    Library("cloudpathlib", "0.25.0", "MIT", "https://cloudpathlib.drivendata.org/"),
    Library("httpx", "0.28.1", "BSD-3-Clause", "https://www.python-httpx.org/"),
    Library("httpcore", "1.0.9", "BSD-3-Clause", "https://www.encode.io/httpcore/"),
    Library("h11", "0.16.0", "MIT", "https://github.com/python-hyper/h11"),
    Library("anyio", "4.14.2", "MIT", "https://anyio.readthedocs.io/"),
    Library("filelock", "3.32.4", "MIT", "https://github.com/tox-dev/filelock"),
    Library("tqdm", "4.70.0", "MPL-2.0 AND MIT", "https://tqdm.github.io/"),
    Library("pydantic-core", "2.46.4", "MIT", "https://github.com/pydantic/pydantic-core"),
    Library("annotated-types", "0.8.0", "MIT", "https://github.com/annotated-types/annotated-types"),
    Library("annotated-doc", "0.0.5", "MIT", "https://github.com/fastapi/annotated-doc"),
    Library("typing-inspection", "0.4.4", "MIT", "https://github.com/pydantic/typing-inspection"),
    Library("typing-extensions", "4.16.0", "PSF-2.0", "https://github.com/python/typing_extensions"),
    # Qt linkes DYNAMISK (Qt-DLL'erne ligger ved siden af exe'en), hvilket er
    # praecis det LGPL-3.0 kraever for at maatte distribuere en lukket/AGPL-app
    # ovenpaa. Bytter man til en statisk Qt-build, bortfalder den ret.
    Library("Qt for Python (PySide6)", "6.9.1", "LGPL-3.0", "https://doc.qt.io/qtforpython/"),
    Library("Qt", "6.9.1", "LGPL-3.0", "https://www.qt.io/"),
    Library("Python", "", "PSF", "https://www.python.org/"),
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
