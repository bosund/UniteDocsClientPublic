"""Opstart.

Raekkefoelgen her er ikke tilfaeldig:

1. **Logning** foerst, saa alt hvad der foelger kan rapportere fejl.
2. **IPC-videresendelse** foer noget UI oprettes -- er der allerede en instans,
   skal filerne bare sendes derover og processen doe. At bygge et vindue foerst
   ville blinke et tomt vindue op.
3. **``QApplication``**, tema og splash.
4. De tunge importer (``main_app`` traekker PyMuPDF ind) sker *bag* splashen.

DPI kraever ingen kode laengere. 8.x kaldte ``SetProcessDpiAwareness(1)`` foer
det foerste Tk-vindue, fordi Tk ellers blev strukket op som en sloeret bitmap --
og bevidst kun *system*-aware, fordi sv-ttk's sprites ikke kunne skalere. Qt er
per-monitor-DPI-aware af sig selv og skalerer baade chrome og vores
vektorikoner skarpt, saa hele workaround'en er vaek.
"""

import multiprocessing
import sys
import time

from __version__ import __version__


def _install_excepthooks():
    """Log ufangede undtagelser fra baade UI- og worker-traade.

    En vinduesbygget exe har ingen konsol, saa alt der gaar til ``stderr``
    forsvinder sporloest. 8.x loeste det ved at overskrive Tk's
    ``report_callback_exception``; her daekker de to standard-hooks baade
    hovedtraaden og enhver ``threading.Thread``.
    """
    import threading
    from app.logging_config import get_logger

    log = get_logger("unitedocs")

    def hook(exc_type, exc, tb):
        log.critical("Ufanget undtagelse", exc_info=(exc_type, exc, tb))

    sys.excepthook = hook
    threading.excepthook = lambda args: log.critical(
        "Ufanget undtagelse i tråden %s", args.thread_name if args.thread else "?",
        exc_info=(args.exc_type, args.exc_value, args.exc_traceback))


def _forward_to_existing(argv) -> bool:
    """True hvis filerne blev sendt til en koerende instans (og vi skal stoppe)."""
    from app import ipc

    result = ipc.try_forward_to_existing(argv)
    if result is True:
        return True

    if result is False and not ipc.try_become_primary():
        # En anden proces vandt mutexen -- vent paa at dens IPC-server er klar.
        for _ in range(60):                    # op til 30 sekunder
            time.sleep(0.5)
            r = ipc.try_forward_to_existing(argv)
            if r is True:
                return True
            if r is False:
                break                          # primaeren er doed -- aabn nyt vindue
    elif result is None:
        # En instans starter allerede (pladsholder i instances.json).
        for _ in range(60):
            time.sleep(0.5)
            r = ipc.try_forward_to_existing(argv)
            if r is True:
                return True
            if r is False:
                break

    # Vi er primaer -- registrér pladsholderen straks, saa sekundaerer ved det.
    ipc.register_starting()
    return False


def main():
    from app.logging_config import configure_logging
    configure_logging()
    _install_excepthooks()

    if sys.argv[1:] and _forward_to_existing(sys.argv[1:]):
        return

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    app = QApplication(sys.argv)
    app.setApplicationName("Unite Docs")
    app.setApplicationVersion(__version__)
    app.setOrganizationName("Unite Apps")

    from app import theme, utils
    from app.config import AppConfig
    # Temaet skal saettes FOER splashen tegnes, ellers blinker den lys op paa en
    # moerk skaerm. "system" foelger Windows' egen lys/moerk-indstilling.
    theme.apply_theme(app, AppConfig().get("General", "theme", fallback="system"))

    # Ét ikon paa applikationen daekker hovedvindue OG alle dialoger. 8.x maatte
    # saette det pr. vindue og hænge en <Map>-binding paa Toplevel-klassen,
    # fordi Tk's ``iconbitmap(default=...)`` maalt med WM_GETICON ikke ramte
    # nogen af dem.
    ico = utils.resource_path("icon.ico")
    if ico.is_file():
        app.setWindowIcon(QIcon(str(ico)))

    splash = _make_splash(app)
    splash.show()
    app.processEvents()

    splash.showMessage(_splash_text("Indlæser systembiblioteker…"),
                       Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter)
    app.processEvents()
    from app.main_app import MainWindow

    window = MainWindow(initial_files=sys.argv[1:])
    window.show()
    splash.finish(window)
    sys.exit(app.exec())


def _splash_text(text: str) -> str:
    from app.localization import LocalizationManager
    return LocalizationManager.get_text(text)


def _make_splash(app):
    """Enkel splash tegnet i kode -- ingen billedfil at holde i sync med temaet."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
    from PySide6.QtWidgets import QSplashScreen

    from app import theme

    ratio = app.primaryScreen().devicePixelRatio() if app.primaryScreen() else 1.0
    w, h = 420, 160
    pm = QPixmap(int(w * ratio), int(h * ratio))
    pm.setDevicePixelRatio(ratio)
    pm.fill(QColor(theme.C["surface"]))

    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(QColor(theme.C["border_strong"]), 1))
    p.drawRect(0, 0, w - 1, h - 1)
    p.setPen(QColor(theme.C["text"]))
    p.setFont(theme.font("title"))
    p.drawText(0, 0, w, h - 40, Qt.AlignmentFlag.AlignCenter, "Unite Docs")
    p.setPen(QColor(theme.C["text_muted"]))
    p.setFont(theme.font("small"))
    p.drawText(0, h - 62, w, 20, Qt.AlignmentFlag.AlignCenter, "v" + __version__)
    p.end()

    splash = QSplashScreen(pm)
    splash.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
    return splash


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
