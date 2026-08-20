


import pikepdf
from pikepdf import Pdf, PasswordError, PdfError
import io
from pathlib import Path
import datetime
import os
import re
from .utils import create_error_pdf
from .logging_config import get_logger

logger = get_logger(__name__)

# Pre-compiled regex for PDF creation date parsing (performance: avoids recompilation on every call)
_CREATION_DATE_RE = re.compile(r"D:(\d{4})(\d{2})(\d{2})")

# Stable internal encryption-status keys. These are used as logic keys throughout
# the app (filtering in _save_dec/_start_check_only/_run_guessing_process) and must
# NOT be translated. The UI translates them to a localized label only at display time
# (see MainApp._enc_status_label). Do not compare against localized strings.
ENC_ENCRYPTED = "ENCRYPTED"
ENC_DECRYPTED = "DECRYPTED"
ENC_NOT_ENCRYPTED = "NOT_ENCRYPTED"
ENC_IMAGE = "IMAGE"
ENC_ERROR = "ERROR"
ENC_UNKNOWN = "UNKNOWN"

def open_with_passwords(path: str, passwords: list[str]) -> Pdf | None:
    try:
        return pikepdf.open(path)
    except PasswordError:
        for pw in passwords:
            try:
                return pikepdf.open(path, password=pw)
            except PasswordError:
                continue
    except (PdfError, FileNotFoundError, PermissionError) as e:
        logger.info("Kunne ikke åbne filen %s: %s", Path(path).name, e)
    except Exception as e:
        logger.error("Unexpected error opening %s: %s", path, e)
    return None

def _image_to_pdf_a4(image_path: str, rotation: int = 0, crop_box: tuple[int, int, int, int] | None = None) -> io.BytesIO | None:
    try:
        from PIL import Image
        from reportlab.pdfgen import canvas
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader

        img = Image.open(image_path)
        if crop_box:
            img = img.crop(crop_box)
        if rotation != 0:
            img = img.rotate(-rotation, expand=True)
        page_w, page_h = A4
        img_w, img_h = img.size
        aspect = img_h / float(img_w)
        new_w = page_w - 20
        new_h = new_w * aspect
        if new_h > page_h - 20:
            new_h = page_h - 20
            new_w = new_h / aspect
        x_centered, y_centered = (page_w - new_w) / 2, (page_h - new_h) / 2
        pdf_buffer = io.BytesIO()
        c = canvas.Canvas(pdf_buffer, pagesize=A4)
        c.drawImage(ImageReader(img), x_centered, y_centered, width=new_w, height=new_h, preserveAspectRatio=True, anchor='c')
        c.showPage()
        c.save()
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
    try:
        pdf = open_with_passwords(path, passwords)
        return str(len(pdf.pages)) if pdf else ""
    except (PdfError, FileNotFoundError, PermissionError):
        return ""
    except Exception:
        return ""

def enc_status(path: str, passwords: list[str]) -> str:
    """Return a stable internal encryption-status key (ENC_* constant), never a
    localized string. The UI translates the key for display only."""
    if Path(path).suffix.lower() != '.pdf':
        return ENC_IMAGE
    try:
        pikepdf.open(path)
        return ENC_NOT_ENCRYPTED
    except PasswordError:
        for pw in passwords:
            try:
                pikepdf.open(path, password=pw)
                return ENC_DECRYPTED
            except PasswordError:
                continue
        return ENC_ENCRYPTED
    except (PdfError, FileNotFoundError, PermissionError):
        return ENC_ERROR
    except Exception:
        return ENC_UNKNOWN

def get_creation_date(path: str, passwords: list[str]) -> str:
    if Path(path).suffix.lower() != '.pdf':
        try:
            return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        except (OSError, FileNotFoundError):
            return ""
    try:
        pdf = open_with_passwords(path, passwords)
        if pdf:
            for key in ("/CreationDate", "CreationDate", "/ModDate", "ModDate"):
                if (val := pdf.docinfo.get(key)):
                    if (m := _CREATION_DATE_RE.match(str(val))):
                        return f"{m[1]}-{m[2]}-{m[3]}"
        return datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
    except (PdfError, FileNotFoundError, PermissionError):
        return ""
    except Exception:
        return ""

def get_pdf_metadata(path: str, passwords: list[str]) -> dict:
    """Batch metadata fetch: opens the PDF once and returns page_count, enc_status and creation_date.

    Performance: replaces three separate calls to get_page_count(), enc_status() and
    get_creation_date() in _refresh_all() — reducing PDF open operations from 3N to N.
    Returns a dict with keys 'page_count' (str), 'enc_status' (str), 'creation_date' (str).
    """
    suffix = Path(path).suffix.lower()
    if suffix != '.pdf':
        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        except (OSError, FileNotFoundError):
            mtime = ""
        return {"page_count": "", "enc_status": ENC_IMAGE, "creation_date": mtime}

    page_count = ""
    enc = ENC_ERROR
    creation_date = ""

    try:
        # Try opening without password first
        try:
            pdf = pikepdf.open(path)
            enc = ENC_NOT_ENCRYPTED
        except PasswordError:
            pdf = None
            enc = ENC_ENCRYPTED
            for pw in passwords:
                try:
                    pdf = pikepdf.open(path, password=pw)
                    enc = ENC_DECRYPTED
                    break
                except PasswordError:
                    continue
        except (PdfError, FileNotFoundError, PermissionError):
            return {"page_count": "", "enc_status": ENC_ERROR, "creation_date": ""}
        except Exception:
            return {"page_count": "", "enc_status": ENC_UNKNOWN, "creation_date": ""}

        if pdf is not None:
            page_count = str(len(pdf.pages))
            for key in ("/CreationDate", "CreationDate", "/ModDate", "ModDate"):
                if (val := pdf.docinfo.get(key)):
                    if (m := _CREATION_DATE_RE.match(str(val))):
                        creation_date = f"{m[1]}-{m[2]}-{m[3]}"
                        break

        if not creation_date:
            try:
                creation_date = datetime.datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
            except (OSError, FileNotFoundError):
                creation_date = ""

    except Exception as e:
        logger.error("Unexpected error in get_pdf_metadata for %s: %s", path, e)

    return {"page_count": page_count, "enc_status": enc, "creation_date": creation_date}

