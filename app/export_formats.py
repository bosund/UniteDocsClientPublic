"""Eksport af PDF-indhold til Markdown, ePub og ren tekst.

Dette modul ejer al afhængighed af pymupdf4llm, EbookLib og python-markdown.
`pdf_utils.py` forbliver ren PDF-manipulation. Alt tekstudtræk sker gennem
PyMuPDF-dokumenter som kalderen allerede har åbnet.

Vigtige designvalg:
- pymupdf4llm pinnes til 0.3.4 (letvægt: kun pymupdf + tabulate). Nyere
  versioner kræver `pymupdf_layout` + onnxruntime + numpy + (til OCR) opencv —
  ~150-280 MB — bevidst fravalgt af hensyn til installationsstørrelsen.
- **OCR** af scannede sider sker via MuPDF's *indbyggede* Tesseract
  (``_ocr_page`` → ``page.get_textpage_ocr``). Det kræver kun sprogfilerne i
  ``client/tessdata`` (dan+eng, ~6 MB) — ikke en ekstern Tesseract-installation
  og ikke den tunge pymupdf4llm-OCR. Mangler tessdata, falder eksporten pænt
  tilbage til en synlig placeholder (se ``_page_state``/``_build_chapter_body``).
- Rotation nulstilles før tekstudtræk (se ``_extract_page_markdown``): en roteret
  side er en visningsindstilling, men flaget forvirrer layout-analysen.
- Tabeller findes med strategien "lines" (se ``TABLE_STRATEGY``), og den
  kolonneoverskrift pymupdf4llm sluger, sættes tilbage (se
  ``_restore_table_headers``).
- En side sat i fastbreddeskrift gengives som et tegngitter i en kodeblok i
  stedet for pymupdf4llm's output (se ``_monospace_grid``).
- HTML-koder fra tabeludtræk (`<br>`) fjernes (se ``_clean_extracted_markdown``),
  og tabeller uden indhold droppes (se ``_drop_empty_tables``).
- Billeder udelades bevidst (``EXPORT_INCLUDE_IMAGES``): ellers ville pymupdf4llm
  skrive hundredvis af sidecar-PNG'er eller oppuste .md-filer med base64.
"""

import os
# Sluk pymupdf/pymupdf4llm's engangs-anbefaling ("Consider using the
# pymupdf_layout package") FØR motoren importeres — den skrives med print() til
# en stdout der ikke findes i den konsol-løse exe. Sættes også i pdf_utils, men
# dette modul kan importeres først i visse stier.
os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")

from dataclasses import dataclass, field
import functools
from pathlib import Path
import re

import pymupdf

from .localization import LocalizationManager
from .logging_config import get_logger

_ = LocalizationManager.get_text
logger = get_logger(__name__)

FORMAT_PDF = "pdf"
FORMAT_MD = "md"
FORMAT_EPUB = "epub"
FORMAT_TXT = "txt"
FORMAT_ORDER = (FORMAT_PDF, FORMAT_MD, FORMAT_EPUB, FORMAT_TXT)

# Billeder eksporteres ikke med tekstformaterne. Se modul-docstringen.
EXPORT_INCLUDE_IMAGES = False

_EXTENSIONS = {
    FORMAT_PDF: ".pdf",
    FORMAT_MD: ".md",
    FORMAT_EPUB: ".epub",
    FORMAT_TXT: ".txt",
}


class ExportError(Exception):
    """Rejses når et dokument ikke kan eksporteres til det ønskede tekstformat."""


class ExportCancelled(Exception):
    """Rejses når brugeren annullerer en igangværende eksport."""


@dataclass
class ExportFormat:
    key: str
    extension: str


EXPORT_FORMATS = {key: ExportFormat(key, _EXTENSIONS[key]) for key in FORMAT_ORDER}


def extension_for(fmt: str) -> str:
    return _EXTENSIONS[fmt]


def format_label(fmt: str) -> str:
    """Oversat menu-etiket. Kaldes ved kaldetidspunkt, så et sprogskifte slår igennem."""
    return {
        FORMAT_PDF: _("PDF"),
        FORMAT_MD: _("Markdown (.md)"),
        FORMAT_EPUB: _("ePub (.epub)"),
        FORMAT_TXT: _("Ren tekst (.txt)"),
    }[fmt]


def format_filetypes(fmt: str):
    """filetypes-liste til asksaveasfilename."""
    label = {
        FORMAT_PDF: _("PDF-filer"),
        FORMAT_MD: _("Markdown-filer"),
        FORMAT_EPUB: _("ePub-filer"),
        FORMAT_TXT: _("Tekstfiler"),
    }[fmt]
    return [(label, "*" + _EXTENSIONS[fmt])]


@dataclass
class Chapter:
    """Ét kapitel i eksporten — typisk én kildefil i en fletning."""
    title: str
    first_page: int = 0             # 0-indekseret, inklusiv
    last_page: int = -1             # eksklusiv; -1 = til dokumentets slut
    error_text: "str | None" = None  # sat i stedet for et sideinterval ved fejl


@dataclass
class ExportResult:
    """Resultat af en dokument-/fileksport."""
    missing_pages: int = 0          # sider uden tekstlag der fik en placeholder


# ---------------------------------------------------------------------------
# Sidetilstand
# ---------------------------------------------------------------------------

def _page_state(page) -> str:
    """'text' | 'needs_ocr' | 'blank'.

    'needs_ocr' = visuelt indhold uden tekstlag (typisk en scanning); indholdet
    er reelt tabt i tekstudtrækket og skal markeres. 'blank' = reelt tom side;
    en placeholder ville bare være støj.
    """
    try:
        if page.get_text("text").strip():
            return "text"
        if page.get_images(full=True) or page.get_drawings():
            return "needs_ocr"
    except Exception:
        # Kan vi ikke afgøre tilstanden, behandl den som manglende indhold.
        return "needs_ocr"
    return "blank"


def _placeholder_text(source_page_number: int) -> str:
    """source_page_number er 1-indekseret sidetal i KILDEFILEN."""
    return _(
        "[Side %s: teksten kunne ikke udtrækkes — siden er sandsynligvis "
        "en scanning uden tekstlag og er derfor ikke med her.]"
    ) % source_page_number


# ---------------------------------------------------------------------------
# Tekstudtræk
# ---------------------------------------------------------------------------

def _cleared_work_doc(doc):
    """Returnér (work, tmp) hvor `work` har al rotation nulstillet.

    En side brugeren har roteret i UI'en har fået sat `/Rotate`; det er en
    visningsindstilling og ændrer ikke tekstens indhold, men det forvirrer både
    pymupdf4llm's layout-analyse (fx en hel side mast ind i én tabelrække med
    `<br>`) og OCR'ens sideorientering. Vi udtrækker/OCR'er derfor altid fra en
    kopi med rotationen nulstillet. PDF-outputtet (som bevarer rotationen)
    påvirkes ikke. `tmp` skal lukkes af kalderen hvis den ikke er None.
    """
    import pymupdf
    if not any(page.rotation for page in doc):
        return doc, None
    tmp = pymupdf.open()
    tmp.insert_pdf(doc)
    for page in tmp:
        if page.rotation:
            page.set_rotation(0)
    return tmp, tmp


# Tabelgenkendelse. pymupdf4llm's standard er "lines_strict", som kun godtager
# celler tegnet med rigtige streger. Danske lønsedler, opgørelser og kontoudtog
# tegner i stedet hver celle som et udfyldt rektangel (zebra-striber) uden en
# eneste streg — med "lines_strict" blev de tabeller slet ikke fundet, og hver
# række endte som en løs tekstlinje i .md-filen. "lines" tæller også
# cellebaggrundene med og genkender netop de tabeller.
TABLE_STRATEGY = "lines"

_MD_KWARGS = dict(
    page_chunks=True,
    # Bemærk: use_ocr findes IKKE i pymupdf4llm 0.3.4. At sende den ville lande i
    # **kwargs og udløse et advarsels-print der crasher den konsol-løse exe. OCR
    # håndteres i stedet direkte via MuPDF's indbyggede Tesseract (se _ocr_page).
    write_images=EXPORT_INCLUDE_IMAGES,
    embed_images=EXPORT_INCLUDE_IMAGES,
    ignore_images=not EXPORT_INCLUDE_IMAGES,
    show_progress=False,          # ville skrive til en konsol der ikke findes
    table_strategy=TABLE_STRATEGY,
)


# Ord regnes for at stå på samme tekstlinje når underkanten er inden for denne
# afstand — nok til at rumme flere skriftstørrelser i én overskriftslinje.
_HEADER_BAND = 2.0


def _md_row(cells) -> str:
    return "|" + "|".join(cells) + "|"


def _swallowed_header_words(page, table):
    """Ordene i tekstlinjen lige over `table`, hvis pymupdf4llm har kasseret den.

    pymupdf4llm dropper enhver tekstlinje der bare *rører* en tabels boks. Er
    cellerne udfyldte rektangler, starter boksen typisk et par tiendedele inde i
    overskriftslinjens underlængder — så forsvinder hele kolonneoverskriften
    ("Lønartsnr. Beskrivelse … Beløb") ud af eksporten, og tabellens første
    datarække bliver forfremmet til overskrift.

    Vi leder kun efter ord der krydser tabellens overkant med tyngdepunktet over
    den: præcis de ord pymupdf4llm har smidt væk. Ord der ligger helt over
    tabellen er stadig med i teksten og må ikke gentages.
    """
    x0, y0, x1, _y1 = table.bbox
    crossing = [
        w for w in page.get_text("words")
        if w[3] > y0 > (w[1] + w[3]) / 2 and w[2] > x0 and w[0] < x1
    ]
    if not crossing:
        return []
    bottom = max(w[3] for w in crossing)
    return sorted((w for w in crossing if abs(w[3] - bottom) <= _HEADER_BAND),
                  key=lambda w: w[0])


def _header_cells_from_words(words, cells) -> list:
    """Fordel ordene på tabellens kolonner efter deres vandrette midtpunkt."""
    out = [""] * len(cells)
    for w in words:
        mid = (w[0] + w[2]) / 2
        for i, cell in enumerate(cells):
            if cell is not None and cell[0] <= mid < cell[2]:
                out[i] = (out[i] + " " + w[4]).strip()
                break
    return out if any(out) else []


def _restore_table_headers(page, text: str) -> str:
    """Giv tabellerne deres rigtige kolonneoverskrifter tilbage.

    Uden det her viser den eksporterede tabel dokumentets første datarække som
    overskrift ("1000 | Gage | …") og resten under "Col4"/"Col5" — mens den
    række der faktisk navngiver kolonnerne er væk. Se
    ``_swallowed_header_words`` for hvorfor den forsvinder.
    """
    if not text or "|---|" not in text:
        return text
    try:
        tables = page.find_tables(strategy=TABLE_STRATEGY).tables
    except Exception as e:
        logger.debug("kunne ikke genfinde tabeller til overskriftsreparation: %s", e)
        return text

    lines = text.split("\n")
    repaired = set()
    for table in tables:
        header = getattr(table, "header", None)
        if header is None or header.external or not table.rows:
            continue        # overskriften er allerede med i outputtet
        words = _swallowed_header_words(page, table)
        if not words:
            continue
        names = _header_cells_from_words(words, table.rows[0].cells)
        if not names:
            continue
        # Sådan skrev pymupdf tabellens overskriftsrække: tomme celler blev til
        # "Col1", "Col2" … Vi genkender linjen på den og skubber den ned som data.
        promoted = _md_row([(n or "Col%d" % (i + 1)).replace("\n", "<br>")
                            for i, n in enumerate(header.names)])
        demoted = _md_row([(n or "").replace("\n", "<br>") for n in header.names])
        new_header = _md_row(names)
        if new_header == promoted:
            continue
        sep = _md_row(["---"] * table.col_count)
        for i, line in enumerate(lines[:-1]):
            if i in repaired or line != promoted or lines[i + 1] != sep:
                continue
            lines[i:i + 2] = [new_header, sep, demoted]
            repaired.update(range(i, i + 3))
            break
    return "\n".join(lines)


def _extract_page_markdown(work, progress=None, cancel=None) -> list[str]:
    """Markdown pr. side fra et (rotations-nulstillet) dokument.

    Uden ``progress``/``cancel`` køres ét kald over hele dokumentet (giver de mest
    konsistente overskriftsniveauer). Med callbacks (interaktiv eksport) køres side
    for side, så der kan vises fremdrift og annulleres undervejs — dette er den
    eneste måde at afbryde pymupdf4llm's ellers blokerende kald."""
    import pymupdf4llm
    n = work.page_count
    if progress is None and cancel is None:
        chunks = pymupdf4llm.to_markdown(work, **_MD_KWARGS)
        return [_page_markdown(work, i, c) for i, c in enumerate(chunks)]
    out = []
    for i in range(n):
        if cancel is not None and cancel():
            raise ExportCancelled()
        chunks = pymupdf4llm.to_markdown(work, pages=[i], **_MD_KWARGS)
        out.append(_page_markdown(work, i, chunks[0]) if chunks else "")
        if progress is not None:
            progress(i + 1, n)
    return out


def _page_markdown(work, page_index: int, chunk) -> str:
    """Én sides rå pymupdf4llm-output gjort klar til eksport.

    En side sat helt i en fastbreddeskrift går uden om pymupdf4llm: dér ER
    tegnenes placering tabellen (se ``_monospace_grid``).
    """
    if page_index < work.page_count:
        page = work[page_index]
        grid = _monospace_grid(page)
        if grid:
            return grid
        text = _restore_trailing_minus(page, chunk.get("text", ""))
        text = _restore_table_headers(page, text)
    else:
        text = chunk.get("text", "")
    return _drop_empty_tables(_clean_extracted_markdown(text))


# ---------------------------------------------------------------------------
# Efterstillet minus
# ---------------------------------------------------------------------------

# pymupdf4llm fjerner orddeling med ``md_string.replace("-\n", "")``
# (pymupdf_rag.py, get_page_output). Det rammer også et beløb med efterstillet
# minus sidst på en linje, som lønsedler skriver fradrag: "592,39-" og næste
# linje bliver til "592,396251 Pension …" — et fradrag bliver til et tillæg.
# Et beløb med decimaler foran bindestregen er aldrig en orddeling, så de
# steder sættes minus og linjeskift ind igen ud fra sidens egne ord. Kun beløb:
# "1990-" og "uge 12-" kan godt være en tankestreg.
_TRAILING_MINUS_RE = re.compile(r"\d[.,]\d+-$")
_MINUS_CONTEXT_WORDS = 3


def _restore_trailing_minus(page, text: str) -> str:
    """Giv beløb med efterstillet minus deres fortegn tilbage.

    Et beløb kan stå både med og uden minus på samme side (A-skat i
    specifikationen og i opsummeringen). En forekomst i teksten rettes derfor
    kun når siden ikke har en fortegnsløs tvilling med samme kontekst; ellers
    tages ét ord mere fra linjen med, og lykkes det ikke, lades den stå.
    Hellere et manglende minus end et forkert.
    """
    if not text or not any(ch.isdigit() for ch in text):
        return text
    try:
        words = page.get_text("words")
    except Exception as e:
        logger.debug("ord til minusreparation fejlede: %s", e)
        return text
    grouped = {}
    for w in words:
        grouped.setdefault((w[5], w[6]), []).append((w[7], w[4]))
    lines = [[t for _n, t in sorted(ws)] for ws in grouped.values()]
    signed = [(ws, i) for ws in lines for i, t in enumerate(ws)
              if _TRAILING_MINUS_RE.search(t)]
    if not signed:
        return text

    def key(ws, i, k):
        """De k ord der slutter i ws[i], med et efterstillet minus skrællet af."""
        last = ws[i][:-1] if _TRAILING_MINUS_RE.search(ws[i]) else ws[i]
        return " ".join(ws[i - k + 1:i] + [last])

    # Fortegnsløse forekomster pr. kontekst: dem må reparationen ikke ramme.
    unsigned = {k: {key(ws, i, k) for ws in lines for i in range(k - 1, len(ws))
                    if ws[i][-1:].isdigit()}
                for k in range(1, _MINUS_CONTEXT_WORDS + 1)}
    contexts = set()
    for ws, i in signed:
        for k in range(1, min(i + 1, _MINUS_CONTEXT_WORDS) + 1):
            candidate = key(ws, i, k)
            if candidate not in unsigned[k]:
                contexts.add(candidate)
                break

    def fix(m):
        rest = m.string[m.end():m.end() + 1]
        if rest in ("", "\n", "|", "<"):
            return m.group("amount") + "-" + m.group("ws")
        # Limet på næste linje; dens indryk hører ikke til.
        return m.group("amount") + "-\n"

    for context in sorted(contexts, key=len, reverse=True):
        # Ikke midt i et andet tal, og ikke allerede med minus.
        # pymupdf4llm's rå tekst kan have flere mellemrum mellem ordene.
        body = r"[ \t]+".join(re.escape(w) for w in context.split(" "))
        pattern = re.compile(r"(?<![\w.,])(?P<amount>" + body
                             + r")(?!-)(?P<ws>[ \t]*)")
        text = pattern.sub(fix, text)
    return text


# ---------------------------------------------------------------------------
# Rammer der ikke er tabeller
# ---------------------------------------------------------------------------

# En ramme med vandrette skillestreger ligner en tabel for strategien "lines":
# rammen om en lønspecifikation blev en 4×3-tabel hvis øverste rækker er én
# flettet celle — og PyMuPDF's ``Table.to_markdown`` kopierer en flettet celle
# ind i alle kolonner (``fill_empty``), så hele specifikationen stod tre gange.
# pymupdf4llm har ingen indstilling til det og læser kun ``find_tables().tables``,
# så rammerne sorteres fra dér. Indholdet går så gennem pymupdf4llm's
# almindelige tekstvej. Målt: rammer har en celle over hele rækken med 9-23
# linjer; rigtige tabeller har ingen sådan række og højst 8 linjer i en celle.
_FRAME_MIN_LINES = 4


def _is_frame(table) -> bool:
    """Er tabellen en ramme om tekst: en række der er én celle over hele
    bredden og rummer flere tekstlinjer. En mellemoverskrift i en rigtig
    tabel spænder også over rækken, men er én eller to linjer."""
    if table.col_count < 2:
        return False
    try:
        rows = table.extract()
    except Exception:
        return False
    for row in rows:
        filled = [c for c in row if c is not None]
        if len(filled) != 1:
            continue
        if sum(1 for l in (filled[0] or "").splitlines() if l.strip()) >= _FRAME_MIN_LINES:
            return True
    return False


def _without_frames(find_tables):
    @functools.wraps(find_tables)
    def find_tables_without_frames(page, *args, **kwargs):
        finder = find_tables(page, *args, **kwargs)
        try:
            finder.tables = [t for t in finder.tables if not _is_frame(t)]
        except Exception as e:
            logger.debug("rammefilter fejlede: %s", e)
        return finder
    find_tables_without_frames._unitedocs_frames = True
    return find_tables_without_frames


# Installeres ved import og gælder hele processen. Kun pymupdf4llm og
# ``_restore_table_headers`` kalder ``find_tables``, og de skal se de samme
# tabeller — ellers parres overskrifterne med de forkerte tabeller.
if not getattr(pymupdf.Page.find_tables, "_unitedocs_frames", False):
    pymupdf.Page.find_tables = _without_frames(pymupdf.Page.find_tables)


# ---------------------------------------------------------------------------
# Sider sat i fastbreddeskrift
# ---------------------------------------------------------------------------

# Lønsedler, kontoudtog og andre udskrifter fra ældre systemer er sat i Courier
# fra øverst til nederst og bygger kolonnerne med mellemrum. pymupdf4llm gør
# dem ulæselige på tre måder på én gang: hver linje bliver til kode med
# mellemrummene slået sammen (kolonnerne er væk), rammen om et afsnit bliver en
# "tabel" hvis flettede celler gentages i hver kolonne (``fill_empty`` i
# PyMuPDF's ``Table.to_markdown``, som pymupdf4llm ikke kan slå fra), og en
# maskeringsbjælke bliver en tom ``|Col1|Col2|``-tabel. I en fastbreddeskrift
# er x-koordinaten et kolonnenummer, så siden kan gengives tabsfrit som tekst.
_MONO_SHARE = 0.9          # andel af sidens tegn der skal være fastbredde
_MONO_MIN_CHARS = 20
_MONO_NAMES = ("courier", "mono", "consol", "lucidaconsole", "letter gothic")


def _is_mono_span(span) -> bool:
    if span["flags"] & pymupdf.TEXT_FONT_MONOSPACED:
        return True
    name = span["font"].lower()
    return any(n in name for n in _MONO_NAMES)


def _monospace_grid(page) -> str:
    """Siden som en kodeblok med tegnene på deres oprindelige kolonne, eller ""
    hvis siden ikke er sat i en fastbreddeskrift."""
    try:
        blocks = page.get_text("rawdict")["blocks"]
    except Exception as e:
        logger.debug("rawdict fejlede: %s", e)
        return ""

    chars = []          # (grundlinje, x, tegn, størrelse)
    mono = total = 0
    for block in blocks:
        for line in block.get("lines", ()):
            if abs(line["dir"][1]) > 0.01:
                continue                    # lodret/skrå tekst passer ikke i gitteret
            for span in line["spans"]:
                is_mono = _is_mono_span(span)
                for ch in span["chars"]:
                    c = ch["c"]
                    if not c.strip():
                        continue
                    total += 1
                    mono += is_mono
                    chars.append((ch["origin"][1], ch["origin"][0], c,
                                  span["size"], ch["bbox"][2] - ch["bbox"][0]))
    if total < _MONO_MIN_CHARS or mono < _MONO_SHARE * total:
        return ""

    # Tegnbredden måles frem for at antage Courier's 0,6 × skriftstørrelse.
    widths = sorted(w for *_r, w in chars if w > 0)
    cw = widths[len(widths) // 2] if widths else 0
    if cw <= 0:
        return ""
    x_min = min(x for _y, x, *_r in chars)

    # Rækker: tegn hvis grundlinjer ligger inden for en tredjedel tegnhøjde.
    chars.sort(key=lambda t: (t[0], t[1]))
    rows = []
    for y, x, c, size, _w in chars:
        if rows and y - rows[-1][0] <= size / 3:
            rows[-1][1].append((x, c))
        else:
            rows.append([y, [(x, c)]])

    gaps = sorted(b[0] - a[0] for a, b in zip(rows, rows[1:]))
    pitch = gaps[len(gaps) // 2] if gaps else 0

    out = []
    prev_y = None
    for y, row in rows:
        if prev_y is not None and pitch and y - prev_y > 1.8 * pitch:
            out.append("")                  # bevar luften mellem afsnit
        prev_y = y
        line = ""
        for x, c in sorted(row):
            col = int(round((x - x_min) / cw))
            # Et tegn der ville overskrive et andet, sættes bag det.
            line += " " * max(0, col - len(line)) + c
        out.append(line.rstrip())
    # Kodeblokke er det eneste i Markdown der bevarer mellemrum.
    return "```\n" + "\n".join(out) + "\n```"


_MD_TABLE_ROW_RE = re.compile(r"^\|.*\|$")
_PLACEHOLDER_CELL_RE = re.compile(r"^(Col\d+)?$")


def _drop_empty_tables(text: str) -> str:
    """Fjern tabeller uden en eneste celle med indhold.

    Typisk en maskeringsbjælke: et udfyldt sort rektangel som strategien "lines"
    tager for en celle. Tilbage står ``|Col1|Col2|`` og tomme rækker.
    """
    if not text or "|---|" not in text:
        return text
    lines = text.split("\n")
    out, i = [], 0
    while i < len(lines):
        if not _MD_TABLE_ROW_RE.match(lines[i]):
            out.append(lines[i])
            i += 1
            continue
        j = i
        while j < len(lines) and _MD_TABLE_ROW_RE.match(lines[j]):
            j += 1
        block = lines[i:j]
        cells = [c.strip() for row in block for c in row.strip("|").split("|")]
        if any(set(c) - {"-", ":"} and not _PLACEHOLDER_CELL_RE.match(c)
               for c in cells):
            out.extend(block)
        i = j
    return "\n".join(out)


# ---------------------------------------------------------------------------
# OCR (MuPDF's indbyggede Tesseract — kræver kun tessdata-sprogfiler)
# ---------------------------------------------------------------------------

# Dansk app: dansk + engelsk. Tesseract er indbygget i MuPDF-biblioteket; vi
# skal kun levere sprogmodellerne (client/tessdata/{dan,eng}.traineddata).
OCR_LANGUAGE = "dan+eng"
OCR_DPI = 200

_ocr_dir_cache = None
_ocr_dir_checked = False


def _tessdata_dir():
    """Sti til de bundtede tessdata-filer, eller None hvis de mangler."""
    global _ocr_dir_cache, _ocr_dir_checked
    if _ocr_dir_checked:
        return _ocr_dir_cache
    _ocr_dir_checked = True
    try:
        from .utils import resource_path
        d = resource_path("tessdata")
        if (d / "dan.traineddata").exists() or (d / "eng.traineddata").exists():
            _ocr_dir_cache = str(d)
    except Exception as e:
        logger.info("Kunne ikke finde tessdata: %s", e)
    return _ocr_dir_cache


def ocr_available() -> bool:
    return _tessdata_dir() is not None


def _ocr_page(page) -> str:
    """OCR én side via MuPDF's indbyggede Tesseract. Returnerer tekst, eller ""
    hvis OCR ikke er tilgængelig eller intet blev genkendt."""
    tess = _tessdata_dir()
    if not tess:
        return ""
    try:
        tp = page.get_textpage_ocr(flags=0, language=OCR_LANGUAGE, dpi=OCR_DPI,
                                   full=True, tessdata=tess)
        return page.get_text(textpage=tp).strip()
    except Exception as e:
        logger.info("OCR fejlede på en side: %s", e)
        return ""


def _ocr_layer_page(page) -> bool:
    """Læg et USYNLIGT OCR-tekstlag oven på en scannet side, så teksten kan søges
    og markeres — uden at røre det oprindelige billede.

    Rotationen nulstilles mens laget lægges (OCR skal læse siden opret og det
    usynlige tekst skal flugte med billedet), og gendannes bagefter, så billede
    og tekstlag roterer sammen. Returnerer True hvis mindst ét ord blev indsat.
    """
    tess = _tessdata_dir()
    if not tess:
        return False
    saved_rot = page.rotation
    try:
        if saved_rot:
            page.set_rotation(0)
        tp = page.get_textpage_ocr(flags=0, language=OCR_LANGUAGE, dpi=OCR_DPI,
                                   full=True, tessdata=tess)
        inserted = 0
        for w in page.get_text("words", textpage=tp):
            x0, y0, x1, y1, txt = w[0], w[1], w[2], w[3], w[4]
            if not txt.strip():
                continue
            try:
                # render_mode=3 = usynlig tekst (hverken fyld eller streg), men
                # den ligger i content-streamet og kan derfor søges/markeres.
                page.insert_text((x0, y1), txt, fontsize=max(1.0, (y1 - y0) * 0.9),
                                 render_mode=3)
                inserted += 1
            except Exception:
                continue
        return inserted > 0
    except Exception as e:
        logger.info("Kunne ikke lægge OCR-tekstlag på en side: %s", e)
        return False
    finally:
        if saved_rot and page.rotation != saved_rot:
            page.set_rotation(saved_rot)


def add_searchable_ocr_layer(doc) -> int:
    """Gør scannede sider søgbare ved at lægge et usynligt OCR-tekstlag på dem.

    Bruges når der gemmes som PDF, så en scanning (eller et indsat billede) får
    et tekstlag mens billedet bevares uændret. Sider der allerede har tekst
    springes over. Returnerer antal sider der fik et tekstlag. Er OCR ikke
    tilgængelig (mangler tessdata), gøres intet og der returneres 0.
    """
    if not ocr_available():
        return 0
    count = 0
    for i in range(doc.page_count):
        page = doc[i]
        if _page_state(page) != "needs_ocr":
            continue
        if _ocr_layer_page(page):
            count += 1
    return count


_BR_RE = re.compile(r"(?i)<br\s*/?>")
_HTML_TAG_RE = re.compile(r"<(?:img|/?(?:span|div|p|b|i|em|strong|font|sub|sup))\b[^>]*>", re.I)


def _clean_extracted_markdown(text: str) -> str:
    """Fjern HTML-koder som pymupdf4llm indsætter (typisk `<br>` i tabelceller).

    `<br>` bliver til et mellemrum, så en flerlinjet tabelcelle bliver til én
    linje — Markdown-tabellen forbliver gyldig og renderer stadig som en tabel,
    men uden rå HTML i outputtet. Øvrige inline-tags fjernes.
    """
    if not text:
        return text
    text = _BR_RE.sub(" ", text)
    text = _HTML_TAG_RE.sub("", text)
    # Ryd dobbelte mellemrum som br→space kan efterlade inde i celler.
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


def _page_range(doc, chapter: Chapter) -> range:
    start = max(0, chapter.first_page)
    end = chapter.last_page if chapter.last_page >= 0 else doc.page_count
    end = min(end, doc.page_count)
    return range(start, end)


@dataclass
class _RenderedChapter:
    title: str
    body: str
    missing: int = 0
    error_text: "str | None" = None


def _build_chapter_body(doc, chapter: Chapter, pages_md: list[str], fmt: str):
    """Byg brødteksten for ét kapitel, kør OCR på scannede sider, og tæl de sider
    der stadig ikke kunne læses.

    Returnerer (body_text, missing_count, had_text). En scannet side (`needs_ocr`)
    OCR'es; lykkes det, indgår teksten som normal brødtekst. Kun sider hvor OCR
    ikke er tilgængelig eller intet genkendte, får en placeholder og tælles som
    manglende.
    """
    parts: list[str] = []
    missing = 0
    had_text = False
    for page_idx in _page_range(doc, chapter):
        state = _page_state(doc[page_idx])
        if state == "text":
            parts.append(pages_md[page_idx].strip())
            had_text = True
        elif state == "needs_ocr":
            ocr_text = _clean_extracted_markdown(_ocr_page(doc[page_idx]))
            if ocr_text.strip():
                parts.append(ocr_text.strip())
                had_text = True
            else:
                missing += 1
                source_page = page_idx - chapter.first_page + 1
                parts.append(_placeholder_marker(_placeholder_text(source_page), fmt))
        # 'blank': tilføj intet
    body = "\n\n".join(p for p in parts if p)
    return body, missing, had_text


def _placeholder_marker(text: str, fmt: str) -> str:
    """En synlig, iøjnefaldende markering — aldrig en fodnote."""
    if fmt == FORMAT_MD or fmt == FORMAT_EPUB:
        # ePub bygges fra Markdown; blockquote + fed bliver til <blockquote><strong>.
        return "> **" + text + "**"
    # Ren tekst: på egen linje.
    return text


# ---------------------------------------------------------------------------
# Skrivere
# ---------------------------------------------------------------------------

def _demote_markdown_headings(body: str) -> str:
    """Skub pymupdf4llm's egne overskrifter ét niveau ned, så kapiteloverskriften
    (##) forbliver øverst i dispositionen."""
    return re.sub(r"(?m)^(#{1,5}) ", r"#\1 ", body)


def _summary_note(missing: int) -> str:
    return _(
        "Bemærk: %s side(r) i dette dokument indeholder ingen tekst der kunne "
        "udtrækkes. De er markeret i teksten nedenfor."
    ) % missing


def _render_markdown(title: str, chapters: list[_RenderedChapter], total_missing: int) -> str:
    multi = len(chapters) > 1
    out: list[str] = []
    if title:
        out.append("# " + title)
    if total_missing > 0:
        out.append("> " + _summary_note(total_missing))
    for ch in chapters:
        if multi:
            out.append("## " + ch.title)
        if ch.error_text:
            out.append("> **" + ch.error_text + "**")
            continue
        body = ch.body
        if multi:
            body = _demote_markdown_headings(body)
        out.append(body)
    return "\n\n".join(part for part in out if part).strip() + "\n"


def _render_text(title: str, chapters: list[_RenderedChapter], total_missing: int) -> str:
    multi = len(chapters) > 1
    out: list[str] = []
    if title:
        out.append(title)
        out.append("=" * len(title))
    if total_missing > 0:
        out.append(_summary_note(total_missing))
    for ch in chapters:
        if multi:
            out.append("")
            out.append("--- " + ch.title + " ---")
        if ch.error_text:
            out.append(ch.error_text)
            continue
        out.append(_strip_markdown_to_text(ch.body))
    # Kun linjeskift: en side i fastbreddeskrift starter med et indryk der er
    # en del af layoutet (se ``_monospace_grid``).
    return "\n\n".join(part for part in out).strip("\r\n") + "\r\n"


_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")


def _flatten_table_line(line: str) -> str:
    """`|a|b|c|` → celler adskilt af tabs, så en tabel bliver læsbar i .txt."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    cells = [c.strip() for c in inner.split("|")]
    return "\t".join(c for c in cells if c)


def _strip_markdown_to_text(body: str) -> str:
    """Fjern Markdown-støj til .txt-visning og gør tabeller læsbare.

    `<br>` er allerede fjernet i _clean_extracted_markdown. Her fladgøres
    tabelrækker (`|...|`) til tab-adskilte celler, og separatorrækker (`|---|`)
    droppes, så outputtet ikke er fuldt af lodrette streger. En kodeblok (en
    side i fastbreddeskrift, se ``_monospace_grid``) står urørt uden hegn.
    """
    lines_out = []
    in_code = False
    for line in body.splitlines():
        if line.startswith("```"):
            in_code = not in_code
            continue                                     # drop hegnet, behold indholdet
        if in_code:
            lines_out.append(line)                       # mellemrummene ER kolonnerne
            continue
        if _TABLE_SEP_RE.match(line):
            continue                                     # drop |---|---| rækker
        if line.lstrip().startswith("|"):
            line = _flatten_table_line(line)
        line = re.sub(r"^#{1,6}\s*", "", line)           # overskrifter
        line = re.sub(r"^>\s?", "", line)                 # blockquotes
        line = re.sub(r"\*\*(.+?)\*\*", r"\1", line)      # fed
        line = re.sub(r"(?<!\*)\*(?!\*)(.+?)\*", r"\1", line)  # kursiv
        lines_out.append(line)
    return "\n".join(lines_out)


_EPUB_CSS = (
    "body { font-family: serif; line-height: 1.4; }\n"
    "blockquote.ud-missing, p.ud-missing {\n"
    "  border: 1px solid #b00; background: #fff3f3; color: #900;\n"
    "  padding: 0.5em 0.8em; margin: 1em 0; font-style: italic;\n"
    "}\n"
)


def _markdown_to_xhtml(md_body: str) -> str:
    import markdown
    from markdown.extensions.tables import TableExtension
    from markdown.extensions.sane_lists import SaneListExtension
    from markdown.extensions.fenced_code import FencedCodeExtension
    # Nuitka-fælde: send udvidelses-INSTANSER, ikke navne — navneopslag via
    # importlib.metadata entry points knækker rutinemæssigt i frosne builds.
    # FencedCode: en side i fastbreddeskrift er én ```-blok (``_monospace_grid``)
    # og skal blive til <pre>, ellers falder kolonnerne sammen.
    html = markdown.markdown(
        md_body,
        output_format="xhtml",   # EPUB 2 kræver velformet XHTML (<br />)
        extensions=[TableExtension(), SaneListExtension(), FencedCodeExtension()],
    )
    # Markér placeholder-blockquotes så CSS'en kan fremhæve dem.
    html = html.replace("<blockquote>", '<blockquote class="ud-missing">')
    # <pre> uden <code>-barn: EbookLib pretty-printer ved gem og indrykker et
    # barn-element — inde i <pre> er det indrykket synligt på første linje.
    html = html.replace("<pre><code>", "<pre>").replace("</code></pre>", "</pre>")
    return html


def _write_epub(out_path: str, title: str, chapters: list[_RenderedChapter], total_missing: int):
    from ebooklib import epub

    book = epub.EpubBook()
    book.set_title(title or Path(out_path).stem)
    try:
        lang = LocalizationManager.get_instance().current_language
    except Exception:
        lang = "da"
    book.set_language(lang or "da")

    css = epub.EpubItem(uid="style", file_name="style/main.css",
                        media_type="text/css", content=_EPUB_CSS)
    book.add_item(css)

    spine = ["nav"]
    toc = []
    multi = len(chapters) > 1

    intro_html = ""
    if total_missing > 0:
        intro_html = '<p class="ud-missing"><strong>%s</strong></p>' % _summary_note(total_missing)

    for i, ch in enumerate(chapters):
        heading = "<h2>%s</h2>" % _xml_escape(ch.title) if multi else ""
        if ch.error_text:
            body_html = '<p class="ud-missing"><strong>%s</strong></p>' % _xml_escape(ch.error_text)
        else:
            body = _demote_markdown_headings(ch.body) if multi else ch.body
            body_html = _markdown_to_xhtml(body)
        page_intro = intro_html if i == 0 else ""
        # Ingen XML-deklaration/DOCTYPE her: EbookLib parser selv body-indholdet
        # (til nav/pagination) og fejler på et fuldt XML-wrappet dokument.
        # EbookLib skriver den korrekte XHTML-header når filen gemmes.
        content = (
            '<html xmlns="http://www.w3.org/1999/xhtml"><head>'
            '<link rel="stylesheet" type="text/css" href="style/main.css"/>'
            "<title>%s</title></head><body>%s%s%s</body></html>"
            % (_xml_escape(ch.title), page_intro, heading, body_html)
        )
        item = epub.EpubHtml(title=ch.title, file_name="chap_%d.xhtml" % i, lang=lang)
        item.content = content
        item.add_item(css)
        book.add_item(item)
        spine.append(item)
        toc.append(item)

    book.toc = tuple(toc)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = spine
    epub.write_epub(out_path, book)


def _xml_escape(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# ---------------------------------------------------------------------------
# Offentlige indgange
# ---------------------------------------------------------------------------

def _render_chapters(doc, chapters: list[Chapter], fmt: str, progress=None, cancel=None):
    # Ét rotations-nulstillet arbejdsdokument bruges til både tekstudtræk og OCR,
    # så sideindekser flugter på tværs af de to.
    work, tmp = _cleared_work_doc(doc)
    try:
        pages_md = _extract_page_markdown(work, progress=progress, cancel=cancel)
        rendered: list[_RenderedChapter] = []
        total_missing = 0
        got_text = False
        for ch in chapters:
            if cancel is not None and cancel():
                raise ExportCancelled()
            if ch.error_text:
                rendered.append(_RenderedChapter(ch.title, "", 0, ch.error_text))
                continue
            body, missing, had_text = _build_chapter_body(work, ch, pages_md, fmt)
            total_missing += missing
            if had_text:
                got_text = True
            rendered.append(_RenderedChapter(ch.title, body, missing))
        return rendered, total_missing, got_text
    finally:
        if tmp is not None:
            tmp.close()


def write_document(fmt: str, *, doc, out_path: str, chapters: list[Chapter], title: str = "",
                   progress=None, cancel=None) -> ExportResult:
    """Skriv et allerede-åbent PyMuPDF-dokument til fmt (md/epub/txt)."""
    if fmt == FORMAT_PDF:
        raise ExportError("write_document er ikke til PDF — brug doc.save() direkte.")
    if fmt not in (FORMAT_MD, FORMAT_EPUB, FORMAT_TXT):
        raise ExportError(_("Ukendt format: %s") % fmt)

    rendered, total_missing, got_text = _render_chapters(doc, chapters, fmt,
                                                         progress=progress, cancel=cancel)
    if cancel is not None and cancel():
        raise ExportCancelled()

    # Er der intet tekstindhold overhovedet (kun placeholdere/fejl), er filen
    # værdiløs — sig det tydeligt frem for at skrive en fil med kun advarsler.
    has_error_only = all(ch.error_text for ch in rendered)
    if not got_text and not has_error_only:
        raise ExportError(_(
            "Der blev ikke fundet nogen tekst i dokumentet. "
            "Filen er sandsynligvis en scanning uden tekstlag."
        ))

    if fmt == FORMAT_MD:
        content = _render_markdown(title, rendered, total_missing)
        Path(out_path).write_text(content, encoding="utf-8", newline="\n")
    elif fmt == FORMAT_TXT:
        content = _render_text(title, rendered, total_missing)
        # UTF-8 med BOM + CRLF — dansk Windows-app, sikrer æøå i ældre værktøjer.
        with open(out_path, "w", encoding="utf-8-sig", newline="") as fh:
            fh.write(content)
    else:  # FORMAT_EPUB
        _write_epub(out_path, title, rendered, total_missing)

    return ExportResult(missing_pages=total_missing)


def write_file(fmt: str, *, src_path: str, out_path: str, passwords: list[str], title: str = "") -> ExportResult:
    """Åbn én kildefil og skriv den til fmt. Bruges af 'Gem enkeltfiler'."""
    from . import pdf_utils
    doc = pdf_utils.open_with_passwords(src_path, passwords)
    if not doc:
        raise ExportError(_("[Filen %s kunne ikke åbnes.]") % Path(src_path).name)
    try:
        chapter = Chapter(title=title or Path(src_path).stem, first_page=0, last_page=-1)
        return write_document(fmt, doc=doc, out_path=out_path, chapters=[chapter], title="")
    finally:
        doc.close()
