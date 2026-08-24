
import sys
from pathlib import Path
import os
import shutil
import time
import io
import pymupdf

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

def install_window_icon(root) -> None:
    """Giv hovedvinduet og alle senere Toplevel-vinduer Unite Docs-ikonet.

    ``iconbitmap(default=...)`` alene er ikke nok. Målt med Win32 ``WM_GETICON``
    sætter ``default=`` hverken et ikon på det hovedvindue der allerede findes,
    når kaldet sker, eller på Toplevel-vinduer der oprettes bagefter — begge
    returnerer ``BIG=0 SMALL=0``, og Windows falder derfor tilbage til Tk's
    klasse-ikon (fjeren). Kun ``iconbitmap(<fil>)`` på det enkelte vindue giver
    vinduet et rigtigt ikon.

    Derfor sættes ikonet her eksplicit på roden, og en klasse-binding på
    ``<Map>`` giver hvert nyt Toplevel-vindue det samme ikon, når det vises.
    Det holder nye dialoger dækket uden at hvert kaldested skal huske det.
    """
    import tkinter as tk  # lazy: utils bruges også fra ikke-GUI-kode

    ico_path = resource_path("icon.ico")
    if not ico_path.is_file():
        logger.info("'icon.ico' not found. Using default icon.")
        return
    ico = str(ico_path)

    def _apply(window) -> None:
        try:
            window.iconbitmap(ico)
        except tk.TclError:
            logger.info("Kunne ikke sætte vinduesikon på %r", window)

    try:
        # default= dækker de vinduer Tk selv laver internt; _apply dækker roden.
        root.iconbitmap(default=ico)
    except tk.TclError:
        logger.info("'icon.ico' could not be loaded. Using default icon.")
        return
    _apply(root)

    def _on_map(event) -> None:
        window = event.widget
        # Tcl-oprettede vinduer kommer igennem som strenge og springes over.
        if not isinstance(window, tk.Toplevel) or getattr(window, "_ud_icon_set", False):
            return
        window._ud_icon_set = True
        # Tooltips og drag-vinduet er overrideredirect: ingen titellinje at
        # sætte et ikon på.
        if window.wm_overrideredirect():
            return
        _apply(window)

    root.bind_class("Toplevel", "<Map>", _on_map, add="+")


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
    """En A4-side der fortæller at filen ikke kunne åbnes. Returneres som PDF-bytes."""
    buf = io.BytesIO()
    doc = pymupdf.open()
    try:
        page = doc.new_page()  # A4 er default (595x842)
        text = "\n".join([fname, "Filen kunne ikke åbnes,", "eller der skete en anden fejl."])
        rect = pymupdf.Rect(50, 300, 545, 500)
        page.insert_textbox(rect, text, fontsize=14, fontname="helv",
                            align=pymupdf.TEXT_ALIGN_CENTER)
        doc.save(buf, garbage=3, deflate=True)
    finally:
        doc.close()
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


def create_text_page_pdf(heading: str, body: str) -> io.BytesIO | None:
    """Byg en eller flere A4-sider med en overskrift og fri brødtekst.

    Bruger ``pymupdf.Story`` frem for ``insert_textbox``: Story ombryder og
    **paginerer** selv, så en lang fritekst aldrig bliver tavst klippet væk.
    (``insert_textbox`` returnerer blot en negativ værdi når teksten ikke
    passer — den gamle forside-kode måtte derfor skrue skriftstørrelsen ned
    i blinde.)

    Returnerer en BytesIO med PDF'en, eller None hvis der intet er at skrive
    eller opbygningen fejler.
    """
    from html import escape

    heading = (heading or "").strip()
    body = body or ""
    if not heading and not body.strip():
        return None

    parts = []
    if heading:
        parts.append("<h1 style=\"text-align:center\">%s</h1>" % escape(heading))
    for para in body.split("\n"):
        # Tomme linjer bevares som luft — ellers kollapser HTML dem.
        parts.append("<p>%s</p>" % (escape(para) if para.strip() else "&nbsp;"))
    html = "<div style=\"font-family:sans-serif;font-size:11pt\">%s</div>" % "".join(parts)

    try:
        buf = io.BytesIO()
        mediabox = pymupdf.paper_rect("a4")
        where = mediabox + (56, 56, -56, -56)
        story = pymupdf.Story(html=html)
        writer = pymupdf.DocumentWriter(buf)
        more = 1
        pages = 0
        while more and pages < 500:          # hård stopklods mod en løbsk løkke
            dev = writer.begin_page(mediabox)
            more, _ = story.place(where)
            story.draw(dev)
            writer.end_page()
            pages += 1
        writer.close()
        buf.seek(0)
        return buf
    except Exception as e:
        logger.error("Kunne ikke bygge indsat side: %s", e)
        return None


INSERTED_DIR_PREFIX = "inserted_pages_"


def new_inserted_pages_dir() -> Path:
    """Session-mappe til de PDF'er "Indsæt side" genererer.

    Egen mappe pr. kørsel, så oprydningen aldrig kan ramme en anden instans'
    filer. ``purge_stale_temp_files`` rører den ikke (den
    kender kun ``split_cache_*`` og ``*.tmp``) — mappen lever derfor hele
    sessionen, hvilket den skal: modellen peger på filerne indtil der gemmes.
    """
    import uuid
    return get_app_data_path(INSERTED_DIR_PREFIX + uuid.uuid4().hex)


def purge_old_inserted_dirs(max_age_s: float = 3600.0) -> None:
    """Fjern efterladte ``inserted_pages_*``-mapper fra tidligere (crashede)
    kørsler. Kaldes ved opstart; aldersgrænsen gør at en kørende instans'
    mappe ikke kan rammes (appen er single-instance)."""
    base = get_app_data_path()
    now = time.time()
    try:
        for item in base.iterdir():
            if not item.is_dir() or not item.name.startswith(INSERTED_DIR_PREFIX):
                continue
            try:
                if now - item.stat().st_mtime > max_age_s:
                    _force_rmtree(item)
            except OSError:
                pass
    except OSError as e:
        logger.warning("Kunne ikke rydde op i indsatte sider: %s", e)


def purge_stale_temp_files(force_all: bool = False, max_age_s: int = 3600) -> None:
    """Ryd forladte midlertidige filer i %APPDATA%/Unite Docs.

    Laa foer i ``thumbnail_manager`` og fulgte naesten med da filvisningen blev
    fjernet -- men ``*.tmp``-grenen rydder ``instances.json.tmp``, som ``ipc.py``
    skriver ved hver enkelt-instans-registrering. Kun ``split_cache_*``-grenen
    doede sammen med Split; de mapper ryddes stadig her, saa en opgradering
    rydder op efter den gamle version.
    """
    try:
        base = get_app_data_path()
        if not base.exists():
            return
        now = time.time()
        for item in base.iterdir():
            try:
                is_split_dir = item.is_dir() and item.name.startswith("split_cache_")
                is_tmp = item.is_file() and item.name.endswith(".tmp")
                if not (is_split_dir or is_tmp):
                    continue
                if not force_all and (now - item.stat().st_mtime) <= max_age_s:
                    continue
                if is_split_dir:
                    _force_rmtree(item)
                else:
                    item.unlink()
            except (PermissionError, OSError):
                pass          # ofte blot en fil der er i brug
    except Exception as e:
        logger.error("Oprydning af midlertidige filer fejlede: %s", e)
