"""Centraliseret logging-opsætning for UniteDocs.

Erstatter spredte ``print()``-kald med en konfigurerbar logger, så output kan
dæmpes i release-builds og skrives til en roterende logfil til fejlsøgning.

Niveau styres af miljøvariablen ``UNITEDOCS_LOG_LEVEL`` (fx ``DEBUG``);
standard er ``WARNING`` så release-builds er stille på konsollen.

Bemærk: modulet importerer kun standardbiblioteket på modulniveau for at undgå
cirkulære imports (``utils`` importeres dovent inde i ``configure_logging``).
"""
import faulthandler
import logging
import os
import sys
import threading
from logging.handlers import RotatingFileHandler

ROOT_NAME = "unitedocs"
LOG_FILENAME = "unitedocs.log"
CRASH_FILENAME = "unitedocs_crash.log"
_configured = False
_crash_file = None  # holdes i live så faulthandler beholder sin fil-descriptor


def get_log_path():
    """Returnér den fulde sti til logfilen (``…/Unite Docs/unitedocs.log``).

    Lazy import af ``utils`` for at undgå cirkulær import. Kaldes både af
    ``configure_logging`` og af UI'et (download/send-log-funktioner).
    """
    from . import utils  # lazy: undgå cirkulær import
    return utils.get_app_data_path() / LOG_FILENAME


def _install_crash_logging(logger):
    """Sørg for at crashes efterlader spor — også dem Python normalt taber.

    1. ``faulthandler`` dumper en C-traceback ved native crashes (fx et segfault
       i pdfium/pikepdf), som almindelig Python-logging ikke kan fange.
    2. ``sys.excepthook`` logger ufangede undtagelser på hovedtråden.
    3. ``threading.excepthook`` logger ufangede undtagelser i worker-tråde.
    """
    global _crash_file
    # 1) Native crashes → dediktet crash-fil (roteres ikke, så tracebacken består).
    try:
        crash_path = get_log_path().with_name(CRASH_FILENAME)
        _crash_file = open(crash_path, "a", encoding="utf-8")
        faulthandler.enable(file=_crash_file, all_threads=True)
    except Exception:
        pass  # best-effort

    # 2) Ufangede undtagelser på hovedtråden.
    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_tb)
            return
        logger.critical("Ufanget undtagelse", exc_info=(exc_type, exc_value, exc_tb))

    sys.excepthook = _excepthook

    # 3) Ufangede undtagelser i tråde (Python 3.8+).
    def _thread_excepthook(args):
        if issubclass(args.exc_type, SystemExit):
            return
        name = args.thread.name if args.thread else "?"
        logger.critical(
            "Ufanget undtagelse i tråd %s", name,
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    try:
        threading.excepthook = _thread_excepthook
    except Exception:
        pass


def configure_logging(level: str | int | None = None, to_file: bool = True) -> logging.Logger:
    """Konfigurer rod-loggeren én gang. Idempotent.

    Kaldes tidligt i opstarten (``unitedocs.main()``).
    """
    global _configured
    logger = logging.getLogger(ROOT_NAME)
    if _configured:
        return logger

    if level is None:
        # INFO som default: så logfilen indeholder normal aktivitet og giver
        # kontekst omkring en eventuel fejl. Med WARNING var filen tom under en
        # fejlfri session. Kan sænkes/hæves via UNITEDOCS_LOG_LEVEL.
        level = os.environ.get("UNITEDOCS_LOG_LEVEL", "INFO").upper()
    logger.setLevel(level)
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)

    if to_file:
        try:
            log_path = get_log_path()
            # Migration: en tidligere fejl kunne oprette en *mappe* ved navn
            # unitedocs.log; ryd op så fil-handleren kan åbne den som fil.
            if log_path.is_dir():
                import shutil
                shutil.rmtree(log_path, ignore_errors=True)
            file_handler = RotatingFileHandler(
                log_path, maxBytes=512 * 1024, backupCount=2, encoding="utf-8"
            )
            file_handler.setFormatter(fmt)
            logger.addHandler(file_handler)
        except Exception:
            # Fil-logging er best-effort; konsollen er stadig aktiv.
            pass

    _configured = True

    # Fang crashes (native + ufangede undtagelser) så de altid efterlader spor.
    _install_crash_logging(logger)

    # Opstartslinje: sikrer at logfilen aldrig er tom og markerer sessionens
    # start (tidspunkt + version), så den seneste kørsel altid kan identificeres.
    try:
        from . import __version__ as _v
        version = getattr(_v, "__version__", _v)
    except Exception:
        version = "?"
    logger.info("=== UniteDocs %s startet (logniveau %s) ===",
                version, logging.getLevelName(logger.level))

    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Hent en child-logger under ``unitedocs``.

    Brug ``get_logger(__name__)`` i hvert modul.
    """
    base = logging.getLogger(ROOT_NAME)
    if not name or name == ROOT_NAME:
        return base
    # Kort, læsbart child-navn (fx "app.main_app" -> "main_app")
    short = name.rsplit(".", 1)[-1]
    return base.getChild(short)
