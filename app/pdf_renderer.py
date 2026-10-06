import os
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Optional
from PIL import Image
import pymupdf

from . import pdf_utils
from . import text_edit
from . import theme
from .logging_config import get_logger

logger = get_logger(__name__)

# MuPDF er ikke garanteret trådsikkert på tværs af dokumenter. Flere
# thumbnail-workers der renderede samtidigt kunne crashe processen med et
# native segfault (intet i Python-loggen). Alle motorkald i appen serialiseres
# gennem denne ene lås — password_guesser importerer den samme.
PDF_LOCK = threading.Lock()

SUPPORTED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}

# Single source of truth for the file-view thumbnail size (was hardcoded 50×50 in
# several places). The page view uses its own, larger tile size.
THUMB_SIZE = (90, 90)


class _OpenDocCache:
    """Tiny LRU of *open* documents so rendering many pages of the same file (the
    page view) does not re-open — and re-parse — the PDF on every single tile
    (measured ~7 ms/open on a 375-page file → the dominant cost when tiles are
    small). ONLY touched while PDF_LOCK is held, so a single cached doc is never
    used by two threads at once (MuPDF is not cross-thread safe)."""

    def __init__(self, limit: int = 2):
        self._items = OrderedDict()     # key -> open Document
        self._limit = limit

    def get_or_open(self, path: str, passwords):
        key = (os.path.normcase(os.path.abspath(path)), tuple(passwords))
        doc = self._items.get(key)
        if doc is not None:
            try:
                _ = doc.page_count          # liveness probe
                self._items.move_to_end(key)
                return doc
            except Exception:
                self._items.pop(key, None)
        doc = pdf_utils.open_with_passwords(path, passwords)
        if doc is not None:
            self._items[key] = doc
            self._items.move_to_end(key)
            while len(self._items) > self._limit:
                _, old = self._items.popitem(last=False)
                try:
                    old.close()
                except Exception:
                    pass
        return doc

    def clear(self):
        for d in self._items.values():
            try:
                d.close()
            except Exception:
                pass
        self._items.clear()


# Guarded by PDF_LOCK (all render_page/render_pages access holds the lock).
_doc_cache = _OpenDocCache()


def clear_doc_cache():
    """Close and drop all cached open documents. Call on shutdown, and whenever a
    held file must be released. Acquires PDF_LOCK so it never races a render."""
    with PDF_LOCK:
        _doc_cache.clear()


def _log(msg: str):
    logger.debug("%s", msg)


def _zoom_for(page, dpi=None, max_px=None, quality=1.3, cap_dpi=200, rect=None):
    """Compute the render zoom factor for a page.

    Rendering cost scales with the pixel count, so for a thumbnail we must NOT
    render a full 150-dpi page and then throw 99% of it away. When ``max_px`` is
    given, the zoom is chosen so the page's LONG side renders at ~``max_px*quality``
    pixels (capped at ``cap_dpi``) — dramatically faster for large/complex pages.
    Otherwise a fixed ``dpi`` (default 100) is used."""
    if max_px:
        # ``rect`` er beskaeringen naar siden er beskaaret: uden den ville en
        # beskaaret side blive maalt paa hele siden og dermed renderet i en
        # brøkdel af den oenskede pixelstoerrelse.
        r = rect if rect is not None else page.rect
        long_pt = max(r.width, r.height) or 1.0
        eff_dpi = min(cap_dpi, max(8.0, max_px * quality * 72.0 / long_pt))
    else:
        eff_dpi = dpi if dpi else 100
    return eff_dpi / 72.0


def _clip_for(page, crop):
    """``PageEdit.crop`` (uroterede kildeenheder, y-ned) -> ``get_pixmap(clip=)``.

    Maalt empirisk: ``clip`` fortolkes i sidens VISNINGS-rum (efter /Rotate), saa
    rektanglet skal gennem ``page.rotation_matrix``. Det raa rektangel giver
    korrekte maal ved 0/180 men beskaerer det forkerte omraade ved 90/270.
    Bemaerk at ``clip`` er relativt til ``page.rect`` og derfor IKKE skal have
    cropbox-forskydningen med (i modsaetning til ``set_cropbox`` ved gem).
    """
    if not crop:
        return None
    try:
        r = (pymupdf.Rect(*crop) * page.rotation_matrix).normalize() & page.rect
        if r.is_empty or not r.is_valid or r.width < 1 or r.height < 1:
            return None
        return r
    except Exception as e:
        _log(f"Kunne ikke omregne beskaering {crop}: {e}")
        return None


def _pixmap_to_image(pix) -> Image.Image:
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


def render_first_page(path: str, passwords: list[str], dpi: int = None,
                      max_px: int = None) -> Optional[Image.Image]:
    """Render side 1 af en PDF (eller åbn et billede) som et PIL-billede.

    Angiv ``max_px`` for at rendere i cirka målstørrelse (hurtigt til thumbnails)
    i stedet for en fast høj ``dpi``.

    MuPDF rasteriserer også scannede sider direkte, så den tidligere
    tredobbelte fallback-kæde (pdfium → pikepdf-resave → udtræk indlejret
    billede) er ikke længere nødvendig.
    """
    suffix = Path(path).suffix.lower()
    if suffix != '.pdf':
        if suffix in SUPPORTED_IMAGE_EXT:
            try:
                return Image.open(path)
            except Exception as e:
                _log(f"Kunne ikke åbne billede: {e}")
                return None
        return None

    with PDF_LOCK:
        doc = pdf_utils.open_with_passwords(path, passwords)
        if not doc:
            _log("Kunne ikke åbne PDF til rendering")
            return None
        try:
            if doc.page_count == 0:
                _log("PDF har 0 sider")
                return None
            page = doc[0]
            zoom = _zoom_for(page, dpi=dpi or 150, max_px=max_px)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            return _pixmap_to_image(pix)
        except Exception as e:
            _log(f"Rendering fejlede: {type(e).__name__}: {e}")
            return None
        finally:
            doc.close()


def _apply_extra_rotation(img: Image.Image, rotation: int) -> Image.Image:
    """Apply an extra viewing rotation (user delta) to an already-rasterised
    image. PIL rotates counter-clockwise, so negate to rotate clockwise."""
    if rotation % 360:
        return img.rotate(-rotation, expand=True)
    return img


def render_page(path: str, passwords: list[str], page_index: int,
                rotation: int = 0, dpi: int = None, max_px: int = None,
                crop=None, text_edits=()) -> Optional[Image.Image]:
    """Render a single PDF page (or an image file's only page) as a PIL image.

    Pass ``max_px`` to render at ~target size (fast for tiles/preview) instead of a
    fixed ``dpi``. ``rotation`` is an extra user viewing delta applied after
    rasterisation. One document open under PDF_LOCK. For rendering many pages of the
    SAME file prefer :func:`render_pages` (opens once).

    ``crop`` er ``PageEdit.crop``: (x0, y0, x1, y1) i uroterede kildeenheder med y
    nedad. For PDF omregnes det til ``get_pixmap(clip=)``, som vil have rektanglet
    i VISNINGS-rummet (efter /Rotate) -- maalt empirisk: ``rect * rotation_matrix``
    giver de rigtige maal ved alle fire /Rotate-vaerdier, mens det raa rektangel
    beskaerer det forkerte omraade ved 90/270. For billeder er enheden PIL-pixels.

    ``text_edits`` er sidens tekstrettelser. De bages ind i en kopi af siden
    foer rasteriseringen, med praecis den kode gem bruger -- et overlay malet
    oven paa den gamle tekst ville ikke kunne vise en farvet baggrund, og det
    ville kunne vise noget andet end det der bliver gemt."""
    suffix = Path(path).suffix.lower()
    if suffix != '.pdf':
        if suffix in SUPPORTED_IMAGE_EXT:
            try:
                img = Image.open(path).convert("RGB")
                if crop:
                    # Billeder: beskaeringen er i PIL-pixels og skal ske FOER
                    # nedskaleringen, ellers passer koordinaterne ikke.
                    img = img.crop(tuple(int(round(v)) for v in crop))
                if max_px:
                    img.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
                return _apply_extra_rotation(img, rotation)
            except Exception as e:
                _log(f"Kunne ikke åbne billede: {e}")
                return None
        return None

    with PDF_LOCK:
        # Cached open doc (reused across tiles of the same file); NOT closed here.
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc:
            return None
        tmp = None
        try:
            if not (0 <= page_index < doc.page_count):
                return None
            page = doc[page_index]
            if text_edits:
                # Det cachede dokument maa ikke aendres: kopiér siden ud.
                tmp = pymupdf.open()
                tmp.insert_pdf(doc, from_page=page_index, to_page=page_index)
                page = tmp[0]
                text_edit.apply(page, text_edits)
            clip = _clip_for(page, crop)
            zoom = _zoom_for(page, dpi=dpi, max_px=max_px, rect=clip)
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), clip=clip,
                                  alpha=False)
            return _apply_extra_rotation(_pixmap_to_image(pix), rotation)
        except Exception as e:
            _log(f"Side-rendering fejlede: {type(e).__name__}: {e}")
            return None
        finally:
            if tmp is not None:
                tmp.close()


def page_words(path, passwords, page_index):
    # Return page.get_text('words') (list of (x0,y0,x1,y1,word,...)) in UNROTATED
    # source-page points (get_text is rotation-invariant). Cheap, no rasterisation;
    # safe on the main thread. Returns [] on failure.
    if Path(path).suffix.lower() != '.pdf':
        return []
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc or not (0 <= page_index < doc.page_count):
            return []
        try:
            return doc[page_index].get_text('words')
        except Exception as e:
            _log(f'page_words fejlede: {e}')
            return []


def page_text_visible(path, passwords, page_index) -> bool:
    """Er sidens tekst synlig (ikke et skjult OCR-lag over en scanning)?
    Afgoer om en maskering maa klippes fri af nabolinjerne -- se
    ``ocr_text.text_layer_visible``. False ved fejl og for billeder."""
    if Path(path).suffix.lower() != '.pdf':
        return False
    from . import ocr_text
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc or not (0 <= page_index < doc.page_count):
            return False
        return ocr_text.text_layer_visible(doc[page_index])


def page_lines(path, passwords, page_index) -> list:
    """Sidens tekstlinjer til tekstrettelse (``text_edit.Line``) i A-space.
    Billigt, ingen rasterisering. [] ved fejl og for billeder."""
    if Path(path).suffix.lower() != '.pdf':
        return []
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc or not (0 <= page_index < doc.page_count):
            return []
        try:
            return text_edit.lines_on_page(doc[page_index])
        except Exception as e:
            _log(f'page_lines fejlede: {e}')
            return []


def plan_text_edit(path, passwords, page_index, line, new_text):
    """``text_edit.plan`` mod kildesiden (skrifterne slaas op i dens
    ressourcer). None ved fejl eller ingen aendring."""
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc or not (0 <= page_index < doc.page_count):
            return None
        try:
            return text_edit.plan(doc[page_index], line, new_text)
        except Exception as e:
            logger.warning("Tekstrettelse kunne ikke beregnes: %s", e)
            return None


def plan_insert_text(path, passwords, page_index, lines, x, y, text, angle=0,
                     origin=None):
    """``text_edit.plan_insert`` mod kildesiden. None ved fejl eller tom tekst."""
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc or not (0 <= page_index < doc.page_count):
            return None
        try:
            return text_edit.plan_insert(doc[page_index], lines, x, y, text,
                                         angle=angle, origin=origin)
        except Exception as e:
            logger.warning("Indsat tekst kunne ikke beregnes: %s", e)
            return None


def page_geometry(path: str, passwords: list[str], page_index: int):
    """Cheap read (no rasterisation) of a page's display geometry for the
    annotation transform: ``(rotate_deg, disp_w, disp_h)`` where disp_* is
    ``page.rect`` (display space, respecting /Rotate). Returns None on failure.
    Uses the shared open-doc cache under PDF_LOCK. Safe to call on the main
    thread — it opens/reads metadata only, never a pixmap (plan gotcha #13)."""
    if Path(path).suffix.lower() != '.pdf':
        return None
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc or not (0 <= page_index < doc.page_count):
            return None
        try:
            page = doc[page_index]
            r = page.rect
            return int(page.rotation) % 360, float(r.width), float(r.height)
        except Exception as e:
            _log(f"page_geometry fejlede: {e}")
            return None


def render_pages(path: str, passwords: list[str], indices, dpi: int = None,
                 max_px: int = None) -> dict:
    """Batch-render several pages of ONE PDF, opening the document a single time
    under PDF_LOCK. Returns ``{page_index: PIL.Image}`` (missing/failed pages are
    omitted). Rotation is NOT applied here — apply the per-page delta at display.
    Pass ``max_px`` for size-driven (fast) rendering."""
    out = {}
    if Path(path).suffix.lower() != '.pdf':
        return out
    with PDF_LOCK:
        doc = _doc_cache.get_or_open(path, passwords)
        if not doc:
            return out
        for idx in indices:
            if not (0 <= idx < doc.page_count):
                continue
            try:
                page = doc[idx]
                zoom = _zoom_for(page, dpi=dpi, max_px=max_px)
                pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
                out[idx] = _pixmap_to_image(pix)
            except Exception as e:
                _log(f"Batch-side {idx} fejlede: {e}")
    return out


def render_thumbnail(path: str, passwords: list[str], size=THUMB_SIZE) -> Optional[Image.Image]:
    img = render_first_page(path, passwords, dpi=72)
    if img is None:
        try:
            img = Image.new('RGB', size, color=theme.C["tile_bg"])
        except Exception:
            return None
    try:
        # Performance: thumbnail() in-place — no copy needed as the original is not reused
        img.thumbnail(size, Image.Resampling.LANCZOS)
        return img
    except Exception as e:
        _log(f"Thumbnail fejl: {e}")
        return None
