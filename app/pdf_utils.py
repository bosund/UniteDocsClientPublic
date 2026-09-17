

import os
# pymupdf4llm/pymupdf skriver ellers en engangs-anbefaling ("Consider using the
# pymupdf_layout package") til stdout. I den byggede exe uden konsol er stdout
# None, og et print() ville crashe. Slå anbefalingen fra før motoren importeres.
os.environ.setdefault("PYMUPDF_SUGGEST_LAYOUT_ANALYZER", "0")

import pymupdf
import io
from pathlib import Path
import datetime
import re
from .logging_config import get_logger

logger = get_logger(__name__)

# MuPDF's C-lag skriver advarsler direkte til stderr. I den byggede exe
# (--windows-console-mode=disable) findes stderr ikke, og skrivningerne er i
# bedste fald spild. Sluk dem her, ét sted, ved import af motoren.
try:
    pymupdf.TOOLS.mupdf_display_errors(False)
except Exception:  # pragma: no cover - defensivt mod API-ændringer
    pass

# A4 i PDF-punkter. PyMuPDF's new_page() bruger samme mål som default, men
# geometriberegningerne nedenfor har brug for tallene eksplicit.
A4_SIZE = (595.0, 842.0)

# Pre-compiled regex for PDF creation date parsing (performance: avoids recompilation on every call)
_CREATION_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")

# Stable internal encryption-status keys. These are used as logic keys throughout
# the app (filtering in _save_dec/_start_check_only/_run_guessing_process) and must
# NOT be translated. The UI translates them to a localized label only at display time
# (the page view shows a padlock icon instead). Never compare against
# localized strings.
ENC_ENCRYPTED = "ENCRYPTED"
ENC_DECRYPTED = "DECRYPTED"
ENC_NOT_ENCRYPTED = "NOT_ENCRYPTED"
ENC_IMAGE = "IMAGE"
ENC_ERROR = "ERROR"
ENC_UNKNOWN = "UNKNOWN"

# Fejltyper fra PyMuPDF der betyder "filen kan ikke læses som PDF".
_OPEN_ERRORS = (
    pymupdf.FileDataError,
    pymupdf.EmptyFileError,
    FileNotFoundError,
    PermissionError,
    ValueError,
)


def open_with_passwords(path: str, passwords: list[str]) -> "pymupdf.Document | None":
    """Åbn en PDF, om nødvendigt ved at prøve brugerens adgangskoder.

    Returnerer et ÅBENT Document — kalderen har ansvaret for at lukke det
    (brug try/finally). Returnerer None hvis filen ikke kan åbnes eller ingen
    adgangskode passer.
    """
    doc = None
    try:
        doc = pymupdf.open(path)
        if not doc.needs_pass:
            return doc
        for pw in passwords:
            if doc.authenticate(pw):
                return doc
        logger.info("Ingen af adgangskoderne passede til %s", Path(path).name)
    except _OPEN_ERRORS as e:
        logger.info("Kunne ikke åbne filen %s: %s", Path(path).name, e)
    except Exception as e:
        logger.error("Unexpected error opening %s: %s", path, e)
    _safe_close(doc)
    return None


def _safe_close(doc) -> None:
    if doc is not None:
        try:
            doc.close()
        except Exception:
            pass


def _a4_placement(size) -> tuple:
    """``(x, y, w, h)`` for et billede af ``size`` centreret paa en A4-side.

    Ligger for sig selv fordi **to** forbrugere skal regne nøjagtig ens:
    :func:`_image_to_pdf_a4`, der bager rasteren, og :func:`image_a4_transform`,
    der mapper annotationer ind i samme rektangel. Divergerer de to, lander
    maskeringer ved siden af det de skal daekke.
    """
    page_w, page_h = A4_SIZE
    img_w, img_h = size
    aspect = img_h / float(img_w)
    new_w = page_w - 20
    new_h = new_w * aspect
    if new_h > page_h - 20:
        new_h = page_h - 20
        new_w = new_h / aspect
    return (page_w - new_w) / 2, (page_h - new_h) / 2, new_w, new_h


def image_a4_transform(image_path: str, rotation: int = 0, crop_box=None):
    """Mapning fra billedets A-space (PIL-pixels) til A4-punkter, eller None.

    Billedsider har ingen ``/Rotate`` og ingen cropbox i det gemte dokument:
    rotation og beskaering **bages ind i rasteren** af :func:`_image_to_pdf_a4`.
    Annotationer er derimod gemt i uroterede PIL-pixels, saa de skal igennem
    praecis samme kaede (crop -> rotation -> skalering -> centrering) for at
    lande rigtigt paa den faerdige side.

    Returnerer en :class:`~app.view_transform.ViewTransform`, som allerede kan
    netop dén kaede — ingen ny rotationsmatematik skrives her.
    """
    try:
        from PIL import Image

        from .view_transform import ViewTransform

        with Image.open(image_path) as im:
            img_w, img_h = im.size
        if crop_box:
            x0, y0, x1, y1 = crop_box
            cw, ch = max(1, int(x1) - int(x0)), max(1, int(y1) - int(y0))
        else:
            cw, ch = img_w, img_h
        # Efter en 90/270-graders drejning bytter siderne plads.
        rot_w, rot_h = (ch, cw) if rotation % 180 == 90 else (cw, ch)
        x, y, new_w, _new_h = _a4_placement((rot_w, rot_h))
        return ViewTransform(img_w, img_h, rotation % 360, new_w / float(rot_w),
                             x, y, crop=crop_box)
    except Exception as e:
        logger.error("Kunne ikke udlede billedtransformen for %s: %s", image_path, e)
        return None


def _image_to_pdf_a4(image_path: str, rotation: int = 0, crop_box: "tuple[int, int, int, int] | None" = None) -> "io.BytesIO | None":
    """Læg et billede centreret på en A4-side og returnér siden som PDF i en BytesIO."""
    try:
        from PIL import Image

        img = Image.open(image_path)
        if crop_box:
            img = img.crop(crop_box)
        if rotation != 0:
            img = img.rotate(-rotation, expand=True)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")

        page_w, page_h = A4_SIZE
        x_centered, y_centered, new_w, new_h = _a4_placement(img.size)

        img_buffer = io.BytesIO()
        img.save(img_buffer, format="PNG")

        pdf_buffer = io.BytesIO()
        doc = pymupdf.open()
        try:
            page = doc.new_page(width=page_w, height=page_h)
            rect = pymupdf.Rect(x_centered, y_centered, x_centered + new_w, y_centered + new_h)
            page.insert_image(rect, stream=img_buffer.getvalue())
            doc.save(pdf_buffer, garbage=3, deflate=True)
        finally:
            doc.close()
        pdf_buffer.seek(0)
        return pdf_buffer
    except (ImportError, FileNotFoundError, PermissionError, OSError) as e:
        logger.error("Fejl ved konvertering af billede %s: %s", image_path, e)
        return None
    except Exception as e:
        logger.error("Uventet fejl ved billede %s: %s", image_path, e)
        return None


def get_page_count(path: str, passwords: list[str]) -> str:
    if Path(path).suffix.lower() != '.pdf':
        return ""
    doc = None
    try:
        doc = open_with_passwords(path, passwords)
        return str(doc.page_count) if doc else ""
    except Exception:
        return ""
    finally:
        _safe_close(doc)


def enc_status(path: str, passwords: list[str]) -> str:
    """Return a stable internal encryption-status key (ENC_* constant), never a
    localized string. The UI translates the key for display only."""
    if Path(path).suffix.lower() != '.pdf':
        return ENC_IMAGE
    doc = None
    try:
        doc = pymupdf.open(path)
        if not doc.needs_pass:
            return ENC_NOT_ENCRYPTED
        for pw in passwords:
            if doc.authenticate(pw):
                return ENC_DECRYPTED
        return ENC_ENCRYPTED
    except _OPEN_ERRORS:
        return ENC_ERROR
    except Exception:
        return ENC_UNKNOWN
    finally:
        _safe_close(doc)


def _creation_date_from_doc(doc) -> str:
    """Træk YYYY-MM-DD ud af dokumentets metadata, eller "" hvis den mangler."""
    meta = doc.metadata or {}
    for key in ("creationDate", "modDate"):
        if (val := meta.get(key)):
            if (m := _CREATION_DATE_RE.match(str(val))):
                return f"{m[1]}-{m[2]}-{m[3]}"
    return ""


def _file_size(path: str) -> int:
    """Filstoerrelse i bytes, 0 hvis den ikke kan laeses. Bruges kun til sortering."""
    try:
        return os.path.getsize(path)
    except (OSError, FileNotFoundError):
        return 0


def _mtime_date(path: str) -> str:
    try:
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except (OSError, FileNotFoundError):
        return ""


def get_creation_date(path: str, passwords: list[str]) -> str:
    if Path(path).suffix.lower() != '.pdf':
        return _mtime_date(path)
    doc = None
    try:
        doc = open_with_passwords(path, passwords)
        if doc and (found := _creation_date_from_doc(doc)):
            return found
        return _mtime_date(path)
    except Exception:
        return ""
    finally:
        _safe_close(doc)


def get_pdf_metadata(path: str, passwords: list[str]) -> dict:
    """Batch metadata fetch: opens the PDF once and returns page_count, enc_status and creation_date.

    Performance: replaces three separate calls to get_page_count(), enc_status() and
    get_creation_date() in _refresh_all() — reducing PDF open operations from 3N to N.
    Returns a dict with keys 'page_count' (str), 'enc_status' (str), 'creation_date' (str)
    and 'size_bytes' (int).
    """
    suffix = Path(path).suffix.lower()
    size = _file_size(path)
    if suffix != '.pdf':
        return {"page_count": "", "enc_status": ENC_IMAGE,
                "creation_date": _mtime_date(path), "size_bytes": size}

    page_count = ""
    enc = ENC_ERROR
    creation_date = ""
    doc = None

    try:
        try:
            doc = pymupdf.open(path)
        except _OPEN_ERRORS:
            return {"page_count": "", "enc_status": ENC_ERROR,
                    "creation_date": "", "size_bytes": size}
        except Exception:
            return {"page_count": "", "enc_status": ENC_UNKNOWN,
                    "creation_date": "", "size_bytes": size}

        readable = True
        if not doc.needs_pass:
            enc = ENC_NOT_ENCRYPTED
        else:
            enc = ENC_ENCRYPTED
            readable = False
            for pw in passwords:
                if doc.authenticate(pw):
                    enc = ENC_DECRYPTED
                    readable = True
                    break

        if readable:
            page_count = str(doc.page_count)
            creation_date = _creation_date_from_doc(doc)

        if not creation_date:
            creation_date = _mtime_date(path)

    except Exception as e:
        logger.error("Unexpected error in get_pdf_metadata for %s: %s", path, e)
    finally:
        _safe_close(doc)

    return {"page_count": page_count, "enc_status": enc,
            "creation_date": creation_date, "size_bytes": size}
