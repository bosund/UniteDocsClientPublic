
import sys
from pathlib import Path
import os
import shutil
import time
import io
import pikepdf
from pikepdf import Pdf
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import A4

from .logging_config import get_logger

logger = get_logger(__name__)

def resource_path(relative_path: str) -> Path:
    if getattr(sys, 'frozen', False):
        # Nuitka compiled exe — sys.executable er altid exe-stien uanset CWD
        base_path = Path(sys.executable).parent
    else:
        # Dev: utils.py er client/app/utils.py → .parent.parent = client/
        base_path = Path(__file__).resolve().parent.parent
    return base_path / relative_path

def _force_rmtree(path: Path, max_retries: int = 5, delay_s: float = 0.1):
    """A more robust version of shutil.rmtree that retries on failure (Fix for Bug 10)."""
    if not path.exists():
        return
        
    for attempt in range(max_retries):
        try:
            # First try to remove read-only attributes on Windows
            if os.name == 'nt':  # Windows
                for root, dirs, files in os.walk(path):
                    for dir_name in dirs:
                        try:
                            os.chmod(os.path.join(root, dir_name), 0o777)
                        except (OSError, PermissionError):
                            pass
                    for file_name in files:
                        try:
                            os.chmod(os.path.join(root, file_name), 0o777)
                        except (OSError, PermissionError):
                            pass
            
            shutil.rmtree(path)
            return
        except (OSError, PermissionError) as e:
            if attempt < max_retries - 1:
                time.sleep(delay_s * (attempt + 1))  # Exponential backoff
            else:
                logger.warning("Failed to remove %s after %s attempts: %s", path, max_retries, e)
                # Final attempt with ignore_errors
                try:
                    shutil.rmtree(path, ignore_errors=True)
                except Exception as final_e:
                    logger.warning("Final cleanup attempt failed for %s: %s", path, final_e)

def create_error_pdf(fname: str) -> io.BytesIO:
    try:
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        for i, txt in enumerate([fname, "Filen kunne ikke åbnes,", "eller der skete en anden fejl."]):
            c.setFont("Helvetica", 14)
            width_a4, _ = A4
            x = (width_a4 - c.stringWidth(txt, "Helvetica", 14)) / 2
            y = A4[1] / 2 - i * 24
            c.drawString(x, y, txt)
        c.showPage()
        c.save()
        buf.seek(0)
        return buf
    except ImportError:
        buf = io.BytesIO()
        Pdf.new().save(buf)
        buf.seek(0)
        return buf

def get_app_data_path(subfolder: str | None = None) -> Path:
    """Get the application data directory path"""
    app_name = "Unite Docs"
    base_path = Path(os.getenv('APPDATA', Path.home())) / app_name
    
    final_path = base_path / subfolder if subfolder else base_path
    try:
        final_path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        logger.error("Kunne ikke oprette app data-mappe: %s", e)
        return Path(os.path.abspath("."))
    return final_path
