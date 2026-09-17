"""Tekstudtraek fra sider — med OCR og orienteringsproeve for scanninger.

Modulet leverer **ord i A-space**: uroterede kildesidepunkter med y nedad, samme
rum som ``AnnotationSpec.rects``, ``PageEdit.crop`` og ``page.search_for()``.
For billedfiler er A-space **PIL-pixels**.

Hvorfor modulet findes
----------------------
``page.get_textpage_ocr()`` er **upaalidelig paa roterede sider**. Maalt mod
PyMuPDF 1.27.2 med samme indhold:

====================  =====================================================
``/Rotate``           resultat af ``get_text("words", textpage=...)``
====================  =====================================================
0                     korrekt tekst
90                    volapyk (``'OU'``, ``'I'``, ``'TT'``, ``'Oo'``)
270                   **nul ord**
====================  =====================================================

PyMuPDF's egen implementering rasteriserer med ``page.get_pixmap(dpi=dpi)`` og
mapper bagefter med ``page.derotation_matrix``; den kombination holder ikke.
Dertil kommer den fysiske variant: en scanning der er fodret paa tvaers giver
Tesseract liggende glyffer, og saa er resultatet volapyk uanset ``/Rotate``.

Derfor rasteriserer vi **selv** i hver kandidatretning, OCR'er en *opret*
midlertidig side, og mapper ordene tilbage til A-space med den eksisterende
:class:`~app.view_transform.ViewTransform`. Der skrives ingen ny
rotationsmatematik her — ``ViewTransform`` er allerede kalibreret empirisk mod
alle 16 kombinationer af ``/Rotate`` og brugerrotation.

Traade
------
Funktionerne roerer MuPDF og skal derfor kaldes med ``pdf_renderer.PDF_LOCK``
holdt. Laasen tages **ikke** her, fordi kalderen skal kunne slippe den mellem
sider — en fuld OCR-koersel varer minutter og maa ikke fryse miniature-optegning.
"""

import os
import re
import threading
from collections import OrderedDict

import pymupdf
from PIL import Image

from .logging_config import get_logger
from .view_transform import ViewTransform

logger = get_logger(__name__)

# Dansk app: dansk + engelsk. Tesseract er indbygget i MuPDF-biblioteket; vi
# skal kun levere sprogmodellerne (client/tessdata/{dan,eng}.traineddata).
OCR_LANGUAGE = "dan+eng"
OCR_DPI = 200

#: Lav oploesning til orienteringsproeven. Vi skal kun *sammenligne* fire
#: retninger, ikke laese teksten praecist, saa 72 dpi er rigeligt og markant
#: billigere end den fulde koersel.
PROBE_DPI = 72

#: Er der mindst saa mange tegn i det native tekstlag, er siden ikke en
#: scanning, og OCR er spild.
MIN_NATIVE_CHARS = 20

#: Scoren en kandidat skal naa for at vi springer de oevrige retninger over.
#: Scoren er kvadratisk i ordlaengden (se :func:`_score`), saa ~6-8 rigtige ord
#: rammer den. En almindelig tekstside rammer den paa foerste kandidat.
ACCEPT_SCORE = 400

#: Korteste token der taeller med i orienteringsscoren. To-tegns stumper er
#: praecis dét roteret tekst falder fra hinanden i, saa de maa ikke give point.
MIN_TOKEN_LEN = 3

#: Rects laengere end saa mange medianlinjehoejder fra spennets foerste rect
#: kasseres — se :func:`_drop_outliers`.
MAX_LINE_GAP = 3.0

#: Et *velformet* ord: Stort-forbogstav, helt smaat, eller HELT STORT. Netop
#: den form roteret tekst ikke har — se :func:`_score`.
_WELL_FORMED = re.compile(
    r"^(?:[A-ZÆØÅ][a-zæøå]+|[a-zæøå]+|[A-ZÆØÅ]+)$", re.UNICODE)

#: Tegnsaetning der maa sidde uden om et ord uden at diskvalificere det.
_TRIM = " \t\r\n.,:;!?\"'()[]{}«»<>-–—/\\*"

_tessdata_cache = None
_tessdata_checked = False


def tessdata_dir():
    """Sti til de bundtede tessdata-filer, eller None hvis de mangler."""
    global _tessdata_cache, _tessdata_checked
    if _tessdata_checked:
        return _tessdata_cache
    _tessdata_checked = True
    try:
        from .utils import resource_path
        d = resource_path("tessdata")
        if (d / "dan.traineddata").exists() or (d / "eng.traineddata").exists():
            _tessdata_cache = str(d)
    except Exception as e:
        logger.info("Kunne ikke finde tessdata: %s", e)
    return _tessdata_cache


def ocr_available() -> bool:
    return tessdata_dir() is not None


def needs_ocr(page) -> bool:
    """True naar siden har visuelt indhold men intet brugbart tekstlag."""
    try:
        if len(page.get_text("text").strip()) >= MIN_NATIVE_CHARS:
            return False
        return bool(page.get_images(full=True) or page.get_drawings())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ord-cache
# ---------------------------------------------------------------------------

class _WordsCache:
    """Traadsikker LRU over udtrukne ordlister.

    OCR koster sekunder pr. side, saa en gentagen anonymisering — eller en
    efterfoelgende "Soeg og masker" paa samme bundt — skal ikke betale igen.
    Noeglen indeholder filens mtime, saa en aendret fil aldrig giver et
    foraeldet svar.
    """

    def __init__(self, limit: int = 64):
        self._d = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key not in self._d:
                return None
            self._d.move_to_end(key)
            return self._d[key]

    def put(self, key, value) -> None:
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._limit:
                self._d.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._d.clear()


_WORDS_CACHE = _WordsCache()


def clear_cache() -> None:
    _WORDS_CACHE.clear()


def _cache_key(path, page_index, rotation_hint):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return (os.path.normcase(os.path.abspath(path)), page_index, mtime,
            rotation_hint % 360)


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------

def _ocr_at(page, extra_deg: int, dpi: int) -> list:
    """Rasteriser ``page`` drejet ``extra_deg`` og OCR den som en opret side.

    ``get_pixmap(matrix=...)`` honorerer sidens eget ``/Rotate``; ``extra_deg``
    laegges oveni. Den midlertidige side bygges i **punkter der svarer 1:1 til
    kildesidens**, saa de returnerede ordkoordinater kan mappes tilbage uden
    skalering (se :func:`_map_words`).
    """
    tess = tessdata_dir()
    if not tess:
        return []
    zoom = dpi / 72.0
    tmp = None
    try:
        m = pymupdf.Matrix(zoom, zoom) * pymupdf.Matrix(extra_deg)
        px = page.get_pixmap(matrix=m, alpha=False)
        tmp = pymupdf.open()
        tp_page = tmp.new_page(width=px.width * 72.0 / dpi,
                               height=px.height * 72.0 / dpi)
        tp_page.insert_image(tp_page.rect, pixmap=px)
        px = None                      # frigiv rasteren straks — den er stor
        # BEMAERK: samme ``tp_page``-objekt til begge kald. ``doc[0]`` bygger et
        # NYT Page-objekt hver gang, og ``TextPage`` holder kun en *svag*
        # reference til sin side — bruger man ``tmp[0]`` to gange, er den foerste
        # side samlet op naar ``get_text`` tjekker ``tp.parent``, og kaldet doer
        # med "ReferenceError: weakly-referenced object no longer exists".
        tp = tp_page.get_textpage_ocr(flags=0, language=OCR_LANGUAGE, dpi=dpi,
                                      full=True, tessdata=tess)
        return list(tp_page.get_text("words", textpage=tp))
    except Exception as e:
        logger.info("OCR ved %d grader fejlede: %s", extra_deg, e)
        return []
    finally:
        if tmp is not None:
            try:
                tmp.close()
            except Exception:
                pass


def _score(words) -> int:
    """Hvor meget ordet-agtig tekst er der i denne retning?

    Et naivt "tael bogstaver"-maal duer **ikke**. Maalt paa en rigtig side laeser
    Tesseract den 180-graders-vendte udgave som lange bogstavsekvenser
    (``'BiyjawWweH'``, ``'UsSsUEer'``) og scorer dermed *hoejere* end den rigtige
    retning. Signalet er ikke maengden af bogstaver, men **formen**: rigtige ord
    er Stort-forbogstav, helt smaa eller HELT STORE, mens vendt tekst giver
    kasus-rod midt i ordet, og sidevaerts tekst falder fra hinanden i to-tegns
    stumper.

    Derfor: kun velformede tokens paa mindst :data:`MIN_TOKEN_LEN` tegn taeller,
    og bidraget er **kvadratisk i laengden**, saa ét rigtigt ord vejer tungere
    end en haandfuld stumper.
    """
    total = 0
    for w in words:
        tok = w[4].strip(_TRIM)
        if len(tok) < MIN_TOKEN_LEN:
            continue
        if _WELL_FORMED.match(tok):
            total += len(tok) ** 2
    return total


def _best_orientation(page, rotation_hint: int, cancel=None):
    """(ord, extra_deg) for den retning der giver mest laesbar tekst."""
    candidates = []
    for deg in (rotation_hint % 360, 0, 90, 180, 270):
        if deg in (0, 90, 180, 270) and deg not in candidates:
            candidates.append(deg)

    best_words, best_deg, best_score = [], 0, -1
    for deg in candidates:
        if cancel is not None and cancel.is_set():
            return [], 0
        words = _ocr_at(page, deg, PROBE_DPI)
        score = _score(words)
        # Uafgjort foretraekker den foerste kandidat, som enten er brugerens
        # eget rotationshint eller 0 grader — begge bedre gaet end 90/270.
        if score > best_score:
            best_words, best_deg, best_score = words, deg, score
        if score >= ACCEPT_SCORE:
            break

    if best_score <= 0:
        return [], 0
    # Proeven koeres kun for at *vaelge* retning; teksten selv laeses igen ved
    # fuld oploesning, hvor genkendelsen er markant bedre.
    full = _ocr_at(page, best_deg, OCR_DPI)
    return (full or best_words), best_deg


def _map_words(words, uw: float, uh: float, total_deg: int) -> list:
    """Map ord fra den oprette temp-side tilbage til A-space.

    ``scale=1.0`` er bevidst: temp-siden blev bygget i ``px.width*72/dpi``
    punkter, saa dens punktkoordinater er allerede lig kildesidens.
    """
    vt = ViewTransform(uw, uh, total_deg, 1.0, 0.0, 0.0)
    out = []
    for w in words:
        ax, ay = vt.canvas_to_pdf(w[0], w[1])
        bx, by = vt.canvas_to_pdf(w[2], w[3])
        out.append((min(ax, bx), min(ay, by), max(ax, bx), max(ay, by),
                    w[4], w[5], w[6], w[7]))
    return out


def page_words_a_space(page, *, allow_ocr: bool = True, rotation_hint: int = 0,
                       cancel=None, cache_path: str = "",
                       cache_index: int = -1) -> list:
    """Ord paa ``page`` i A-space, om noedvendigt via OCR.

    Returnerer PyMuPDF's 8-tuple ``(x0, y0, x1, y1, tekst, block, line, word)``
    uanset om ordene kom fra tekstlaget eller fra OCR, saa de to stier er
    udskiftelige for kalderen.
    """
    try:
        native = list(page.get_text("words"))
    except Exception as e:
        logger.info("get_text('words') fejlede: %s", e)
        native = []

    if sum(len(w[4].strip()) for w in native) >= MIN_NATIVE_CHARS:
        return native                       # allerede A-space, og gratis
    if not allow_ocr or not ocr_available():
        return native

    key = None
    if cache_path and cache_index >= 0:
        key = _cache_key(cache_path, cache_index, rotation_hint)
        hit = _WORDS_CACHE.get(key)
        if hit is not None:
            return hit

    words, deg = _best_orientation(page, rotation_hint, cancel=cancel)
    if not words:
        return native

    dw, dh = page.rect.width, page.rect.height
    uw, uh = (dh, dw) if page.rotation % 180 == 90 else (dw, dh)
    mapped = _map_words(words, uw, uh, (page.rotation + deg) % 360)
    if key is not None:
        _WORDS_CACHE.put(key, mapped)
    return mapped


def image_words_a_space(path: str, *, rotation_hint: int = 0, cancel=None) -> list:
    """Ord i et billede, i **PIL-pixels** (billedernes A-space).

    Billedet laegges paa en side hvor 1 px = 1 pt. ``pymupdf.open(path)`` bruges
    bevidst **ikke**: en PNG med en ``pHYs``-chunk giver en side-rect der ikke er
    pixel-lig, og saa ville rects lande forskudt i forhold til ``PageEdit.crop``,
    der regner i pixels.
    """
    if not ocr_available():
        return []
    key = _cache_key(path, -2, rotation_hint)
    hit = _WORDS_CACHE.get(key)
    if hit is not None:
        return hit

    doc = None
    try:
        with Image.open(path) as im:
            w, h = im.size
        doc = pymupdf.open()
        page = doc.new_page(width=w, height=h)
        page.insert_image(page.rect, filename=path)
        words, deg = _best_orientation(doc[0], rotation_hint, cancel=cancel)
        if not words:
            return []
        mapped = _map_words(words, float(w), float(h), deg % 360)
        _WORDS_CACHE.put(key, mapped)
        return mapped
    except Exception as e:
        logger.info("Kunne ikke OCR'e billedet %s: %s", path, e)
        return []
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Ord <-> tekst
# ---------------------------------------------------------------------------

def words_to_text(words):
    """Byg soegbar tekst af ordlisten og et indeks tilbage til ordene.

    Returnerer ``(tekst, offsets)`` hvor ``offsets`` er
    ``[(start, slut, ord_indeks), ...]``. Linjer adskilles med linjeskift og
    blokke med et blankt linjeskift, saa en NER-model ser noget der ligner
    afsnit frem for én endeloes streng.
    """
    parts = []
    offsets = []
    pos = 0
    prev_block = prev_line = None

    for i, w in enumerate(words):
        text = w[4]
        if not text:
            continue
        block, line = w[5], w[6]
        if prev_block is None:
            sep = ""
        elif block != prev_block:
            sep = "\n\n"
        elif line != prev_line:
            sep = "\n"
        else:
            sep = " "
        if sep:
            parts.append(sep)
            pos += len(sep)
        parts.append(text)
        offsets.append((pos, pos + len(text), i))
        pos += len(text)
        prev_block, prev_line = block, line

    return "".join(parts), offsets


def rects_for_span(words, offsets, start: int, end: int) -> list:
    """Rects i A-space for tegnspennet ``[start, end)``.

    Ét rect pr. ``(block, line)`` spennet beroerer — altsaa ét ved et normalt
    hit og N naar hittet braekker over linjer.
    """
    hits = [idx for (s, e, idx) in offsets if s < end and e > start]
    if not hits:
        return []

    lines = OrderedDict()
    for idx in hits:
        w = words[idx]
        lines.setdefault((w[5], w[6]), []).append(w)

    rects = []
    for group in lines.values():
        rects.append((min(w[0] for w in group), min(w[1] for w in group),
                      max(w[2] for w in group), max(w[3] for w in group)))

    rects = _drop_outliers(rects)
    pad = 1.0
    return [(r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad) for r in rects]


def _drop_outliers(rects: list) -> list:
    """Kassér rects langt fra det foerste.

    OCR'ens laeseraekkefoelge kan flette to spalter sammen, saa et spen kommer
    til at spaende over indhold der visuelt ligger langt fra hinanden. Uden
    dette vaern ville en enkelt fejlflettet linje svaerte urelateret tekst.
    """
    if len(rects) < 2:
        return rects
    heights = sorted(r[3] - r[1] for r in rects)
    median = heights[len(heights) // 2] or 1.0
    limit = MAX_LINE_GAP * median * max(1, len(rects))
    first_y = rects[0][1]
    kept = [r for r in rects if abs(r[1] - first_y) <= limit]
    if len(kept) != len(rects):
        logger.info("Kasserede %d rect(s) langt fra spennets foerste linje",
                    len(rects) - len(kept))
    return kept or rects[:1]
