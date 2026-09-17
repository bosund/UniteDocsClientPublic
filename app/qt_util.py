"""Smaa Qt-byggeklodser der bruges paa tvaers af vinduer og dialoger.

Her ligger de tre ting migrationen fra Tk gjorde brug for igen og igen:

* **``MainThreadInvoker``** -- broen fra worker-traade til UI-traaden. Den
  afloeser 8.x' ``queue.Queue`` + ``after(50, _process_queue)``-pumpe, men
  beholder dens ``put((fn, args))``-API, saa ``page_render`` og alle
  worker-funktioner er uaendrede. Forskellen er at et resultat nu leveres i det
  oejeblik det er klart i stedet for op til 50 ms senere -- det er det der gør
  scroll og rendering maerkbart mere flydende.
* **Dialog-hjaelpere** med *oversatte* knaptekster. Qt's standardknapper hedder
  "OK"/"Cancel" paa systemets sprog, ikke appens; 8.x byggede derfor egne
  Toplevels for at kunne oversaette dem. Her tilfoejes knapperne i stedet med
  ``addButton(tekst, rolle)``, saa ``_()`` styrer teksten og Qt styrer resten.
* **``LinkLabel``** -- den klikbare, understregede label appen bruger som link.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
                               QMessageBox, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from . import theme
from .localization import LocalizationManager
from .logging_config import get_logger

logger = get_logger(__name__)
_ = LocalizationManager.get_text


# --------------------------------------------------------------------------
# Traad -> UI
# --------------------------------------------------------------------------
class MainThreadInvoker(QObject):
    """Koer et kald paa UI-traaden, uanset hvilken traad der beder om det.

    ``put((fn, args))`` maa kaldes fra en hvilken som helst traad. Signalet er
    forbundet med ``Qt.QueuedConnection``, saa Qt selv lægger kaldet i
    UI-traadens eventkoe -- ingen polling, ingen laas, og ingen risiko for at
    en worker roerer en widget.

    ``put``-navnet (og tuple-formen) er bevaret fra ``queue.Queue``, saa
    ``page_render.PageRenderManager`` og alle worker-funktioner virker uaendret.
    """

    _posted = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._posted.connect(self._run, Qt.ConnectionType.QueuedConnection)
        self._alive = True

    def put(self, item) -> None:
        fn, args = item
        if self._alive:
            self._posted.emit(fn, args)

    def shutdown(self) -> None:
        """Stop leveringen. Kaldes ved lukning, saa et sent worker-resultat ikke
        vaekker widgets der er ved at blive revet ned."""
        self._alive = False

    def _run(self, fn, args) -> None:
        try:
            fn(*args)
        except Exception:
            logger.exception("Ufanget undtagelse i UI-callback %r", getattr(fn, "__name__", fn))


def run_in_thread(fn, *args, name: str | None = None, **kwargs) -> threading.Thread:
    """Start en daemon-worker. Findes for at samle navngivning og daemon-flaget
    ét sted -- traadene selv er uaendrede fra 8.x."""
    t = threading.Thread(target=fn, args=args, kwargs=kwargs, daemon=True, name=name)
    t.start()
    return t


# --------------------------------------------------------------------------
# Links og labels
# --------------------------------------------------------------------------
class LinkLabel(QLabel):
    """Klikbar, understreget label i ``C["link"]``.

    En ``QLabel`` med rig tekst og ``<a href>`` ville ogsaa virke, men saa
    bestemmer Qt farven og teksten skal escapes -- her er den oversatte streng
    ren tekst, og udseendet kommer fra tokens.
    """

    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None,
                 url: str | None = None):
        super().__init__(text, parent)
        self._url = url
        self.setObjectName("Link")
        self.setFont(theme.font("link"))
        self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        if url:
            self.clicked.connect(lambda: open_url(url))

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt-API
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.pos()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def set_inert(self, text: str) -> None:
        """Gør linket til almindelig daempet tekst (fx efter et engangs-klik)."""
        self.setText(text)
        self.setObjectName("Muted")
        self.setFont(theme.font("small"))
        self.setCursor(QCursor(Qt.CursorShape.ArrowCursor))
        self.setEnabled(False)
        self.style().unpolish(self)
        self.style().polish(self)


def open_url(url: str) -> None:
    from PySide6.QtCore import QUrl
    try:
        QDesktopServices.openUrl(QUrl(url))
    except Exception as e:
        logger.warning("Kunne ikke aabne %s: %s", url, e)


def muted_label(text: str, *, small: bool = True, parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setObjectName("Muted")
    if small:
        lbl.setFont(theme.font("small"))
    return lbl


# --------------------------------------------------------------------------
# Beskeder. Knaptekster gaar gennem _(), ikke gennem Qt's systemsprog.
# --------------------------------------------------------------------------
def _box(parent, icon, title: str, text: str) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(icon)
    box.setWindowTitle(title)
    box.setText(text)
    return box


def info(parent, title: str, text: str) -> None:
    box = _box(parent, QMessageBox.Icon.Information, title, text)
    box.addButton(_("OK"), QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def warn(parent, title: str, text: str) -> None:
    box = _box(parent, QMessageBox.Icon.Warning, title, text)
    box.addButton(_("OK"), QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def error(parent, title: str, text: str) -> None:
    box = _box(parent, QMessageBox.Icon.Critical, title, text)
    box.addButton(_("OK"), QMessageBox.ButtonRole.AcceptRole)
    box.exec()


def ask_choice(parent, title: str, text: str, choices, *, default: int = 0):
    """Spoerg med vilkaarligt mange oversatte knapper.

    ``choices`` er ``[(noegle, tekst), ...]``; returnerer noeglen for den knap
    brugeren trykkede, eller ``None`` hvis dialogen blev lukket. Qt's egne
    standardknapper bruges bevidst ikke — de oversaettes til *systemets* sprog,
    ikke appens (CLAUDE.md regel 6).
    """
    box = _box(parent, QMessageBox.Icon.Question, title, text)
    buttons = []
    for i, (key, label) in enumerate(choices):
        role = (QMessageBox.ButtonRole.AcceptRole if i == 0
                else QMessageBox.ButtonRole.RejectRole)
        buttons.append((key, box.addButton(label, role)))
    if 0 <= default < len(buttons):
        box.setDefaultButton(buttons[default][1])
    box.exec()
    clicked = box.clickedButton()
    for key, btn in buttons:
        if btn is clicked:
            return key
    return None


def ask_yes_no(parent, title: str, text: str, *, dangerous: bool = False) -> bool:
    """Ja/nej med oversatte knapper. ``dangerous`` gør Nej til standardvalget."""
    box = _box(parent, QMessageBox.Icon.Warning if dangerous else QMessageBox.Icon.Question,
               title, text)
    yes = box.addButton(_("Ja"), QMessageBox.ButtonRole.YesRole)
    no = box.addButton(_("Nej"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(no if dangerous else yes)
    box.exec()
    return box.clickedButton() is yes


# --------------------------------------------------------------------------
# Fremdrift
# --------------------------------------------------------------------------
class ProgressDialog(QDialog):
    """Modal fremdriftsdialog med forloebet tid og valgfri annullering.

    Afloeser 8.x' ``_show_progress_dialog``, som returnerede en **dict med rå
    widget-referencer** der rejste med ind i worker-traadene. Her ser workeren
    kun ``cancel_event`` (et ``threading.Event``); alt widget-arbejde sker i
    metoderne, som kun kaldes paa UI-traaden gennem ``MainThreadInvoker``.
    """

    def __init__(self, parent, title: str, text: str, *, cancellable: bool = False):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        # Ingen ? -knap i titellinjen; luk styres af annullér-knappen.
        self.setWindowFlag(Qt.WindowType.WindowContextHelpButtonHint, False)
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, cancellable)

        self.cancel_event: threading.Event | None = threading.Event() if cancellable else None
        self._conv_start: float | None = None
        self._start = _now()

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 14)
        lay.setSpacing(theme.SPACE["sm"])

        head = QLabel(text, self)
        head.setWordWrap(True)
        head.setFont(theme.font("heading"))
        lay.addWidget(head)

        self._status = QLabel(_("Initialiserer..."), self)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        self._bar = QProgressBar(self)
        self._bar.setRange(0, 100)
        self._bar.setTextVisible(False)
        lay.addWidget(self._bar)

        self._elapsed = muted_label("", parent=self)
        lay.addWidget(self._elapsed)

        self._cancel_btn: QPushButton | None = None
        if cancellable:
            box = QDialogButtonBox(self)
            self._cancel_btn = box.addButton(_("Annuller"),
                                             QDialogButtonBox.ButtonRole.RejectRole)
            self._cancel_btn.clicked.connect(self.request_cancel)
            lay.addWidget(box)

        from PySide6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(1000)
        self._tick()

    # -- UI-traad --------------------------------------------------------
    def _tick(self) -> None:
        self._elapsed.setText(_("Forløbet: %s") % fmt_secs(_now() - self._start))

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_fraction(self, done: float, total: float) -> None:
        if total:
            self._bar.setRange(0, 100)
            self._bar.setValue(int(round(done / total * 100)))
        else:
            self._bar.setRange(0, 0)          # ubestemt

    def set_busy(self) -> None:
        self._bar.setRange(0, 0)

    def set_bytes(self, done, total) -> None:
        if total:
            self.set_fraction(done, total)
            self.set_status(_("Hentet %s af %s") % (fmt_bytes(done), fmt_bytes(total)))
        else:
            self.set_busy()
            self.set_status(_("Hentet %s") % fmt_bytes(done))

    def set_page_progress(self, done: int, total: int) -> None:
        """Per-side fremdrift med ETA (tekstkonvertering er den langsomme del)."""
        if self._conv_start is None:
            self._conv_start = _now()
        self.set_fraction(done, total)
        eta = ""
        if 0 < done < total:
            elapsed = _now() - self._conv_start
            eta = " · " + (_("ca. %s tilbage") % fmt_secs(elapsed / done * (total - done)))
        self.set_status((_("Konverterer side %(d)s af %(t)s")
                         % {"d": done, "t": total}) + eta)

    def request_cancel(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.set_status(_("Annullerer…"))
        if self._cancel_btn is not None:
            self._cancel_btn.setEnabled(False)

    def finish(self) -> None:
        """Luk dialogen. Sikker at kalde flere gange."""
        self._timer.stop()
        self.done(0)
        self.deleteLater()

    # -- Qt-hooks --------------------------------------------------------
    def reject(self) -> None:  # Escape / luk-knap
        if self.cancel_event is not None:
            self.request_cancel()
        # Ellers ignoreres lukningen: arbejdet koerer videre og skal ses.

    def closeEvent(self, event):  # noqa: N802 - Qt-API
        if self.cancel_event is not None:
            self.request_cancel()
        event.ignore()


# --------------------------------------------------------------------------
# Formattering
# --------------------------------------------------------------------------
def _now() -> float:
    import time
    return time.time()


def fmt_bytes(num) -> str:
    """``24941445`` -> ``"23,8 MB"`` (dansk decimalkomma)."""
    try:
        value = float(num or 0)
    except (TypeError, ValueError):
        return "-"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(value)
            return ("%.1f %s" % (value, unit)).replace(".", ",")
        value /= 1024.0
    return "-"


def fmt_secs(secs) -> str:
    secs = int(secs)
    if secs < 60:
        return "%ds" % secs
    return "%dm %02ds" % (secs // 60, secs % 60)


def name_filter(label: str, patterns) -> str:
    """Byg ét Qt-filter: ``("PDF-filer", ["*.pdf"])`` -> ``"PDF-filer (*.pdf)"``."""
    if isinstance(patterns, str):
        patterns = patterns.split()
    return "%s (%s)" % (label, " ".join(patterns))


def button_row(parent, *buttons, align_right: bool = True) -> QWidget:
    """Vandret knaprække med Windows' afstand. Sidste knap er den primaere."""
    row = QWidget(parent)
    lay = QHBoxLayout(row)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(theme.SPACE["sm"])
    if align_right:
        lay.addStretch(1)
    for b in buttons:
        lay.addWidget(b)
    return row
