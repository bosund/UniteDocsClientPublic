import io
import threading
from pathlib import Path
from typing import Optional
from PIL import Image
import pikepdf

from .logging_config import get_logger

logger = get_logger(__name__)

try:
    import pypdfium2 as pdfium
except ImportError:  # Fallback: user must install pypdfium2
    pdfium = None

# PDFium er IKKE trådsikkert: dets funktioner må ikke kaldes samtidigt fra flere
# tråde — heller ikke på forskellige dokumenter (jf. pypdfium2-dokumentationen,
# "Incompatibility with Threading"). Flere thumbnail-workers der renderede
# samtidigt kunne derfor crashe processen med et native segfault (intet i
# Python-loggen). Alle pdfium-kald i appen serialiseres gennem denne ene lås.
PDFIUM_LOCK = threading.Lock()

SUPPORTED_IMAGE_EXT = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}


def _log(msg: str):
    logger.debug("%s", msg)


def _render_with_pdfium_path(path: str, passwords: list[str], dpi: int) -> Optional[Image.Image]:
    if not pdfium:
        _log("pypdfium2 ikke installeret")
        return None
    first_err: Optional[Exception] = None
    # Serialisér alle pdfium-kald (se PDFIUM_LOCK) — pdfium er ikke trådsikkert.
    with PDFIUM_LOCK:
        # Prøv med tom adgangskode først og derefter brugerens liste
        for pw in ["", *passwords, None]:
            doc = None
            try:
                if pw is None:
                    doc = pdfium.PdfDocument(path)
                else:
                    doc = pdfium.PdfDocument(path, password=pw)
                if len(doc) == 0:
                    _log("PdfDocument har 0 sider")
                    return None
                page = doc[0]
                pil_image = page.render(scale=dpi/72).to_pil()
                return pil_image.convert('RGB')
            except Exception as e:
                # Gem første fejl til loggen: for en fil der slet ikke kan
                # åbnes/renderes er fejlen typisk ens for alle password-forsøg.
                if first_err is None:
                    first_err = e
                continue
            finally:
                if doc is not None:
                    doc.close()
    if first_err is not None:
        _log(f"Ingen password virkede via pdfium ({type(first_err).__name__}: {first_err})")
    else:
        _log("Ingen password virkede via pdfium")
    return None


def _extract_first_image_with_pikepdf(path: str, passwords: list) -> Optional[Image.Image]:
    """Fallback: hent første indlejrede billede i side 1 (ofte brugbar for scannede PDF'er).

    'passwords' may be a pre-expanded list (e.g. ["", *original_passwords]) when called
    from render_first_page() to avoid repeated list construction. Entries must be
    str — pikepdf.open() rejects password=None with a TypeError.
    """
    for pw in passwords:
        try:
            with pikepdf.open(path, password=pw) as pdf:
                if not pdf.pages:
                    return None
                page = pdf.pages[0]
                resources = page.get('/Resources', {})
                xobjects = resources.get('/XObject') if resources else None
                if not xobjects:
                    continue
                for name, obj in xobjects.items():
                    try:
                        if getattr(obj, 'get', lambda k, d=None: None)('/Subtype') == '/Image':
                            img_data = obj.read_bytes()
                            # Forsøg at åbne direkte
                            bio = io.BytesIO(img_data)
                            try:
                                return Image.open(bio)
                            except Exception:
                                # Hvis komprimeret: gem via pikepdf helper
                                pass
                    except Exception:
                        continue
        except pikepdf.PasswordError:
            continue
        except Exception:
            return None
    return None


def render_first_page(path: str, passwords: list[str], dpi: int = 150) -> Optional[Image.Image]:
    suffix = Path(path).suffix.lower()
    if suffix != '.pdf':
        if suffix in SUPPORTED_IMAGE_EXT:
            try:
                return Image.open(path)
            except Exception as e:
                _log(f"Kunne ikke åbne billede: {e}")
                return None
        return None

    # Performance: precompute expanded password list once for all fallback paths below.
    # Tom streng først (= "intet password" for pikepdf) — ALDRIG None: pikepdf.open()
    # kaster TypeError på password=None, og da det ikke er en PasswordError, ville
    # den afbryde hele fallback-kæden før de rigtige passwords blev prøvet.
    pikepdf_passwords: list[str] = list(dict.fromkeys(["", *passwords]))

    # Først direkte pdfium
    img = _render_with_pdfium_path(path, passwords, dpi)
    if img:
        return img

    # Fallback: forsøg at åbne med pikepdf og gemme til buffer og så pdfium (nogle gange virker det)
    if pdfium:
        try:
            for pw in pikepdf_passwords:
                try:
                    with pikepdf.open(path, password=pw) as pdf:
                        bio = io.BytesIO()
                        pdf.save(bio)
                        bio.seek(0)
                        # Serialisér pdfium-kaldene (ikke trådsikkert, se PDFIUM_LOCK).
                        with PDFIUM_LOCK:
                            doc = None
                            try:
                                doc = pdfium.PdfDocument(bio)
                                if len(doc):
                                    page = doc[0]
                                    img2 = page.render(scale=dpi/72).to_pil().convert('RGB')
                                    return img2
                            except Exception:
                                continue
                            finally:
                                if doc is not None:
                                    doc.close()
                except pikepdf.PasswordError:
                    continue
        except Exception as e:
            _log(f"Fallback pikepdf->pdfium fejlede: {e}")

    # Sidste fallback: forsøg at udtrække første billede (reuse precomputed list)
    img = _extract_first_image_with_pikepdf(path, pikepdf_passwords)
    if img:
        return img

    _log("Alle render-forsøg mislykkedes")
    return None


def render_thumbnail(path: str, passwords: list[str], size=(50, 50)) -> Optional[Image.Image]:
    img = render_first_page(path, passwords, dpi=72)
    if img is None:
        try:
            img = Image.new('RGB', size, color='#f0f0f0')
        except Exception:
            return None
    try:
        # Performance: thumbnail() in-place — no copy needed as the original is not reused
        img.thumbnail(size, Image.Resampling.LANCZOS)
        return img
    except Exception as e:
        _log(f"Thumbnail fejl: {e}")
        return None
