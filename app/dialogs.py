"""Appens dialoger.

Laa foer inde i ``main_app.py`` som 15 ``tk.Toplevel``-opbygninger paa ~1200
linjer. De er flyttet ud hertil som ``QDialog``-klasser, fordi hovedvinduet
ellers ville vaere naesten uarbejdeligt -- og fordi en dialog nu er et objekt
med metoder i stedet for en dict af raa widget-referencer.

Faellestraek, som alle dialoger her holder sig til:

* **Modale dialoger bruger ``exec()``.** Det afloeser Tk's
  ``grab_set()`` + ``wait_window()``-par, og Qt haandterer selv indlejrede
  eventloops og fokus.
* **Ingen manuel centrering.** Qt placerer en dialog paa sin ``parent``; 8.x'
  ``winfo_rootx()``-regnestykker er vaek.
* **Knaptekster gaar gennem ``_()``**, aldrig gennem Qt's standardknapper --
  dem oversaetter Qt til *systemets* sprog, ikke appens. Se ``qt_util``.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFrame,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton,
                               QScrollArea, QSpinBox, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from . import (anonymize, context_menu, credits, export_formats, pii, theme,
               updater)
from . import __version__
from .config import AppConfig
from .localization import LocalizationManager
from .logging_config import get_logger
from .qt_util import (LinkLabel, button_row, fmt_bytes, muted_label, open_url)
from . import qt_util

logger = get_logger(__name__)
_ = LocalizationManager.get_text


def _body(dialog: QDialog, margins=(18, 16, 18, 12), spacing=None) -> QVBoxLayout:
    """Standard-indmad: samme marginer i alle dialoger, saa de foles ens."""
    lay = QVBoxLayout(dialog)
    lay.setContentsMargins(*margins)
    lay.setSpacing(theme.SPACE["md"] if spacing is None else spacing)
    return lay


def _separator(parent=None) -> QFrame:
    return theme.hairline("horizontal", parent)


# ==========================================================================
# Opdatering
# ==========================================================================
class UpdateDialog(QDialog):
    """Ny version fundet. ``run()`` giver ``"install"``, ``"later"`` eller ``"skip"``."""

    def __init__(self, parent, info):
        super().__init__(parent)
        self.setWindowTitle(_("Opdatering tilgængelig"))
        self.setModal(True)
        self._choice = "later"

        lay = _body(self)
        title = QLabel(_("Der er en ny version af Unite Docs"), self)
        title.setFont(theme.font("title"))
        lay.addWidget(title)

        sub = QLabel(_("Version %s er klar. Du kører version %s.")
                     % (info.version, __version__), self)
        sub.setWordWrap(True)
        lay.addWidget(sub)

        if info.size:
            lay.addWidget(muted_label(_("Download: %s") % fmt_bytes(info.size), parent=self))

        if info.release_notes:
            head = QLabel(_("Nyt i denne version:"), self)
            head.setFont(theme.font("strong"))
            lay.addWidget(head)
            notes = QPlainTextEdit(self)
            notes.setPlainText(info.release_notes)
            notes.setReadOnly(True)
            notes.setMinimumSize(QSize(460, 170))
            lay.addWidget(notes, 1)

        lay.addWidget(_separator(self))
        skip = QPushButton(_("Spring denne version over"), self)
        later = QPushButton(_("Ikke nu"), self)
        install = QPushButton(_("Opdater nu"), self)
        install.setDefault(True)
        skip.clicked.connect(lambda: self._choose("skip"))
        later.clicked.connect(lambda: self._choose("later"))
        install.clicked.connect(lambda: self._choose("install"))

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(theme.SPACE["sm"])
        rl.addWidget(skip)
        rl.addStretch(1)
        rl.addWidget(later)
        rl.addWidget(install)
        lay.addWidget(row)

    def _choose(self, value: str) -> None:
        self._choice = value
        self.accept()

    def reject(self) -> None:
        self._choice = "later"
        super().reject()

    def run(self) -> str:
        self.exec()
        return self._choice


class UpdateLinkDialog(QDialog):
    """Fejl-/fallback-dialog med klikbart link til hentesiden."""

    def __init__(self, parent, title: str, message: str, info, extra_path=None,
                 on_open_file=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        url = getattr(info, "info_url", None) or updater.INFO_URL

        lay = _body(self)
        msg = QLabel(message, self)
        msg.setWordWrap(True)
        msg.setMinimumWidth(440)
        lay.addWidget(msg)

        link = LinkLabel(url, self, url=url)
        link.setWordWrap(True)
        lay.addWidget(link)

        if extra_path is not None:
            file_link = LinkLabel(str(extra_path), self)
            file_link.setWordWrap(True)
            if on_open_file is not None:
                file_link.clicked.connect(lambda: on_open_file(Path(extra_path)))
            lay.addWidget(file_link)

        lay.addWidget(_separator(self))
        ok = QPushButton(_("Luk"), self)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        lay.addWidget(button_row(self, ok))
        open_url(url)


# ==========================================================================
# Indsæt side
# ==========================================================================
class InsertPageDialog(QDialog):
    """Editor til en ny side: overskrift + fritekst. Ikke-modal, ét eksemplar."""

    submitted = Signal(str, str)

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(_("Indsæt side"))
        self.setModal(False)
        self.resize(520, 460)

        lay = _body(self)
        lay.addWidget(QLabel(_("Overskrift"), self))
        self._head = QLineEdit(self)
        lay.addWidget(self._head)

        lay.addWidget(QLabel(_("Tekst"), self))
        self._text = QPlainTextEdit(self)
        # Ombryder som den faerdige side gør; ``undo`` er indbygget i Qt.
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        lay.addWidget(self._text, 1)

        cancel = QPushButton(_("Annuller"), self)
        ok = QPushButton(_("Indsæt"), self)
        ok.setDefault(True)
        cancel.clicked.connect(self.close)
        ok.clicked.connect(self._submit)
        lay.addWidget(button_row(self, cancel, ok))
        self._head.setFocus()

    def _submit(self) -> None:
        heading = self._head.text().strip()
        body = self._text.toPlainText()
        if not heading and not body.strip():
            qt_util.info(self, _("Indsæt side"),
                         _("Skriv en overskrift eller noget tekst først."))
            return
        self.close()
        self.submitted.emit(heading, body)


def ask_before_after(parent, question: str) -> str | None:
    """Modal: Før / Efter / Annuller. Returnerer ``"before"``, ``"after"`` eller None.

    Egen dialog frem for ``QMessageBox.question``, fordi de tre valg ikke er et
    ja/nej og fordi knapteksterne skal gennem ``_()``.
    """
    dlg = QDialog(parent)
    dlg.setWindowTitle(_("Indsæt side"))
    dlg.setModal(True)
    lay = _body(dlg)
    lbl = QLabel(question, dlg)
    lbl.setWordWrap(True)
    lbl.setMinimumWidth(380)
    lay.addWidget(lbl)

    result = {"v": None}

    def choose(v):
        result["v"] = v
        dlg.accept()

    cancel = QPushButton(_("Annuller"), dlg)
    before = QPushButton(_("Før"), dlg)
    after = QPushButton(_("Efter"), dlg)
    after.setDefault(True)
    cancel.clicked.connect(lambda: choose(None))
    before.clicked.connect(lambda: choose("before"))
    after.clicked.connect(lambda: choose("after"))
    lay.addWidget(button_row(dlg, cancel, before, after))
    dlg.exec()
    return result["v"]


# ==========================================================================
# Kvittering efter gem
# ==========================================================================
class SavedDialog(QDialog):
    """Kvittering med et KLIKBART link til resultatet.

    En almindelig beskedboks kan kun vise stien som tekst; her kan man klikke
    sig direkte hen til filen (eller mappen, naar der blev gemt flere).
    """

    def __init__(self, parent, title: str, message: str, target: Path, *,
                 is_folder: bool, on_open):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        target = Path(target)

        lay = _body(self)
        msg = QLabel(message, self)
        msg.setWordWrap(True)
        msg.setMinimumWidth(420)
        lay.addWidget(msg)

        link = LinkLabel(target.name if not is_folder else str(target), self)
        link.setWordWrap(True)
        link.clicked.connect(lambda: on_open(target, not is_folder))
        lay.addWidget(link)
        lay.addWidget(muted_label(
            _("Klik for at åbne mappen") if is_folder else _("Klik for at åbne filen"),
            parent=self))

        lay.addWidget(_separator(self))
        ok = QPushButton(_("Luk"), self)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        lay.addWidget(button_row(self, ok))


# ==========================================================================
# Formatvalg
# ==========================================================================
def ask_export_format(parent, default=export_formats.FORMAT_PDF) -> str | None:
    """Modal med radioknapper. Returnerer format-noeglen eller None ved annullering."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(_("Vælg format"))
    dlg.setModal(True)
    lay = _body(dlg)

    head = QLabel(_("Vælg outputformat:"), dlg)
    head.setFont(theme.font("heading"))
    lay.addWidget(head)

    buttons = {}
    for key in export_formats.FORMAT_ORDER:
        rb = QRadioButton(export_formats.format_label(key), dlg)
        rb.setChecked(key == default)
        buttons[key] = rb
        lay.addWidget(rb)

    hint = muted_label(_("Tekstformater bevarer ikke billeder og layout."), parent=dlg)
    hint.setWordWrap(True)
    hint.setMaximumWidth(300)
    lay.addWidget(hint)

    result = {"fmt": None}

    def ok():
        for key, rb in buttons.items():
            if rb.isChecked():
                result["fmt"] = key
                break
        dlg.accept()

    cancel_btn = QPushButton(_("Annuller"), dlg)
    ok_btn = QPushButton(_("OK"), dlg)
    ok_btn.setDefault(True)
    cancel_btn.clicked.connect(dlg.reject)
    ok_btn.clicked.connect(ok)
    lay.addWidget(button_row(dlg, cancel_btn, ok_btn))
    dlg.exec()
    return result["fmt"]


# ==========================================================================
# Credits
# ==========================================================================
class CreditsDialog(QDialog):
    """Licens, kildekodetilbud (AGPL §6) og bibliotekliste."""

    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle(_("Credits"))
        self.setModal(True)
        self.resize(520, 480)

        lay = _body(self)
        name = QLabel("Unite Docs " + __version__, self)
        name.setFont(theme.font("title"))
        lay.addWidget(name)
        lay.addWidget(QLabel("© 2025 Bo Sundgaard", self))

        lic = QLabel(_("Licens: AGPL-3.0"), self)
        lic.setFont(theme.font("strong"))
        lay.addWidget(lic)

        # AGPL §6 kildekodetilbud -- det eneste ikke-valgfrie compliance-element.
        lay.addWidget(LinkLabel(_("Vis kildekode"), self, url=credits.SOURCE_URL))

        libs = QLabel(_("Anvendte biblioteker"), self)
        libs.setFont(theme.font("heading"))
        lay.addWidget(libs)

        inner = QWidget(self)
        il = QVBoxLayout(inner)
        il.setContentsMargins(0, 0, theme.SPACE["md"], 0)
        il.setSpacing(theme.SPACE["sm"])
        for lib in credits.CREDITS:
            ver = (" " + lib.version) if lib.version else ""
            head = QLabel("%s%s" % (lib.name, ver), inner)
            head.setFont(theme.font("strong"))
            il.addWidget(head)
            il.addWidget(muted_label(lib.license + "  —  " + lib.url, parent=inner))
        il.addStretch(1)

        area = QScrollArea(self)
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(area, 1)

        ok = QPushButton(_("OK"), self)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        lay.addWidget(button_row(self, ok))


# ==========================================================================
# Sorterings-panel
# ==========================================================================
class SortPanel(QWidget):
    """Sorterings-panelet under Sorter-knappen.

    Det skal blive staaende mens man klikker flere gange (skift noegle, vend
    retning) -- derfor er det ikke en menu, som lukker ved foerste klik. Som
    ``Qt.Popup`` faar det til gengaeld noget 8.x' ``overrideredirect``-Toplevel
    ikke havde: Qt griber musen, saa et klik *uden for* panelet lukker det, og
    panelet foelger vinduet af sig selv.
    """

    chosen = Signal(str)

    def __init__(self, parent, rows, directions):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)

        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(theme.SPACE["xs"], theme.SPACE["xs"],
                              theme.SPACE["xs"], theme.SPACE["xs"])
        fl.setSpacing(0)
        lay.addWidget(frame)

        for key, label, is_directional in rows:
            btn = QPushButton(_(label), frame)
            btn.setObjectName("CmdButton")
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setStyleSheet("text-align: left; padding: 6px 10px;")
            if is_directional:
                btn.setText("%s   %s" % (_(label), "▼" if directions.get(key) else "▲"))
            btn.clicked.connect(lambda _c=False, k=key: self.chosen.emit(k))
            fl.addWidget(btn)

    def popup_under(self, widget: QWidget) -> None:
        """Vis panelet lige under ``widget``, venstrestillet med den."""
        pos = widget.mapToGlobal(widget.rect().bottomLeft())
        self.adjustSize()
        self.move(pos.x(), pos.y() + 2)
        self.show()


# ==========================================================================
# Kodeord: gætning
# ==========================================================================
class GuessOptionsDialog(QDialog):
    """Vælg gætte-metode og kodelaengde. ``run()`` giver ``(modes, max_len)`` eller None."""

    def __init__(self, parent, default_len: int = 5):
        super().__init__(parent)
        self.setWindowTitle(_("Vælg metode"))
        self.setModal(True)
        lay = _body(self)

        head = QLabel(_("Vælg type af gætning:"), self)
        head.setFont(theme.font("heading"))
        lay.addWidget(head)

        self._numeric = QCheckBox(_("Numerisk (korte koder)"), self)
        self._numeric.setChecked(True)
        lay.addWidget(self._numeric)

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel(_("Antal cifre:"), row))
        self._len = QSpinBox(row)
        self._len.setRange(1, 10)
        self._len.setValue(int(default_len))
        rl.addWidget(self._len)
        rl.addStretch(1)
        lay.addWidget(row)

        cancel = QPushButton(_("Annuller"), self)
        start = QPushButton(_("Start"), self)
        start.setDefault(True)
        cancel.clicked.connect(self.reject)
        start.clicked.connect(self._start)
        lay.addWidget(button_row(self, cancel, start))
        self._result = None

    def _start(self) -> None:
        if not self._numeric.isChecked():
            qt_util.warn(self, _("Fejl"), _("Vælg mindst én metode."))
            return
        self._result = ({"numeric": True}, self._len.value())
        self.accept()

    def run(self):
        self.exec()
        return self._result


class GuessProgressDialog(QDialog):
    """Fremdrift pr. fil under kodeords-tjek/gaet."""

    closed = Signal()

    def __init__(self, parent, title: str, items, name_for):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(False)
        self.resize(720, 520)
        self._stop_event = threading.Event()
        self._rows: dict[str, tuple[QProgressBar, QLabel]] = {}

        lay = _body(self)
        inner = QWidget(self)
        grid = QGridLayout(inner)
        grid.setContentsMargins(0, 0, theme.SPACE["md"], 0)
        grid.setHorizontalSpacing(theme.SPACE["lg"])
        grid.setColumnStretch(0, 1)
        grid.setColumnMinimumWidth(2, 180)

        for r, iid in enumerate(items):
            grid.addWidget(QLabel(name_for(iid), inner), r, 0)
            bar = QProgressBar(inner)
            bar.setFixedWidth(200)
            bar.setRange(0, 100)
            bar.setValue(0)
            bar.setTextVisible(False)
            grid.addWidget(bar, r, 1)
            lbl = QLabel(_("Vent..."), inner)
            grid.addWidget(lbl, r, 2)
            self._rows[iid] = (bar, lbl)
        grid.setRowStretch(len(items), 1)

        area = QScrollArea(self)
        area.setWidget(inner)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.Shape.NoFrame)
        lay.addWidget(area, 1)

        self._stop_btn = QPushButton(_("Stop"), self)
        self._stop_btn.clicked.connect(self.request_stop)
        lay.addWidget(button_row(self, self._stop_btn))

    # -- UI-traad --------------------------------------------------------
    @property
    def stop_event(self) -> threading.Event:
        return self._stop_event

    def request_stop(self) -> None:
        self._stop_event.set()

    def start_row(self, iid: str) -> None:
        row = self._rows.get(iid)
        if row:
            row[0].setRange(0, 0)          # ubestemt = arbejder

    def stop_row(self, iid: str) -> None:
        row = self._rows.get(iid)
        if row:
            row[0].setRange(0, 100)
            row[0].setValue(100)

    def set_row_status(self, iid: str, text: str) -> None:
        row = self._rows.get(iid)
        if row:
            row[1].setText(text)

    def mark_finished(self) -> None:
        """Naar arbejdet er slut bliver Stop til Luk."""
        self._stop_btn.setText(_("Luk"))
        try:
            self._stop_btn.clicked.disconnect()
        except (RuntimeError, TypeError):
            pass
        self._stop_btn.clicked.connect(self.close)
        self._stop_btn.setDefault(True)

    def closeEvent(self, event):  # noqa: N802 - Qt-API
        self._stop_event.set()
        self.closed.emit()
        super().closeEvent(event)


# ==========================================================================
# Kodeord: liste og prompt
# ==========================================================================
class PasswordsDialog(QDialog):
    """Se/redigér kodeordslisten manuelt + Tjek/Gaet over alle krypterede filer."""

    committed = Signal(list)
    check_requested = Signal()
    guess_requested = Signal()

    def __init__(self, parent, lines: list[str]):
        super().__init__(parent)
        self.setWindowTitle(_("Adgangskoder"))
        self.setModal(False)
        self.resize(420, 380)

        lay = _body(self)
        lay.addWidget(QLabel(
            _("Bruges til at åbne krypterede PDF'er. Ét kodeord pr. linje."), self))

        self._text = QPlainTextEdit(self)
        self._text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._text.setPlainText("\n".join(lines))
        self._text.setFont(theme.font("base"))
        lay.addWidget(self._text, 1)

        check = QPushButton(_("Tjek kodeliste"), self)
        guess = QPushButton(_("Gæt kodeord"), self)
        ok = QPushButton(_("OK"), self)
        ok.setDefault(True)
        check.clicked.connect(lambda: (self._commit(), self.check_requested.emit()))
        guess.clicked.connect(lambda: (self._commit(), self.guess_requested.emit()))
        ok.clicked.connect(self.close)

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(theme.SPACE["sm"])
        rl.addWidget(check)
        rl.addWidget(guess)
        rl.addStretch(1)
        rl.addWidget(ok)
        lay.addWidget(row)
        self._text.setFocus()

    def _commit(self) -> None:
        lines = [l.strip() for l in self._text.toPlainText().splitlines() if l.strip()]
        self.committed.emit(list(dict.fromkeys(lines)))

    def sync_lines(self, lines: list[str]) -> None:
        """Gen-synk feltet fra app-tilstanden (fx naar et gaettet kodeord findes).

        Markoerens position bevares, saa et fund midt i en indtastning ikke
        river tekstfeltet fra brugeren."""
        cursor = self._text.textCursor()
        pos = cursor.position()
        self._text.setPlainText("\n".join(lines))
        cursor = self._text.textCursor()
        cursor.setPosition(min(pos, len(self._text.toPlainText())))
        self._text.setTextCursor(cursor)

    def closeEvent(self, event):  # noqa: N802 - Qt-API
        self._commit()
        super().closeEvent(event)


class PasswordPromptDialog(QDialog):
    """Bed om kodeord til én krypteret fil."""

    def __init__(self, parent, filename: str, verify):
        super().__init__(parent)
        self.setWindowTitle(_("Kodeord påkrævet"))
        self.setModal(True)
        self._verify = verify
        self.password: str | None = None
        self.guess_requested = False

        lay = _body(self)
        msg = QLabel(_("Filen \"%s\" er beskyttet med kodeord.") % filename, self)
        msg.setWordWrap(True)
        msg.setMinimumWidth(360)
        lay.addWidget(msg)

        self._entry = QLineEdit(self)
        self._entry.setEchoMode(QLineEdit.EchoMode.Password)
        # Qt's indbyggede oeje-knap afloeser 8.x' "Vis kodeord"-afkrydsning.
        self._entry.returnPressed.connect(self._submit)
        lay.addWidget(self._entry)

        show = QCheckBox(_("Vis kodeord"), self)
        show.toggled.connect(
            lambda on: self._entry.setEchoMode(QLineEdit.EchoMode.Normal if on
                                               else QLineEdit.EchoMode.Password))
        lay.addWidget(show)

        self._err = QLabel("", self)
        self._err.setObjectName("Danger")
        lay.addWidget(self._err)

        skip = QPushButton(_("Spring over"), self)
        guess = QPushButton(_("Gæt kodeord"), self)
        unlock = QPushButton(_("Lås op"), self)
        unlock.setDefault(True)
        skip.clicked.connect(self.reject)
        guess.clicked.connect(self._guess)
        unlock.clicked.connect(self._submit)

        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(theme.SPACE["sm"])
        rl.addWidget(skip)
        rl.addStretch(1)
        rl.addWidget(guess)
        rl.addWidget(unlock)
        lay.addWidget(row)
        self._entry.setFocus()

    def _submit(self) -> None:
        pw = self._entry.text()
        if not pw:
            return
        if self._verify(pw):
            self.password = pw
            self.accept()
        else:
            self._err.setText(_("Forkert kodeord. Prøv igen."))
            self._entry.clear()
            self._entry.setFocus()

    def _guess(self) -> None:
        self.guess_requested = True
        self.reject()


# ==========================================================================
# Indstillinger
# ==========================================================================
class SettingsDialog(QDialog):
    """Indstillinger. Apply-on-close, som i 8.x.

    Layoutet er ét ``QGridLayout`` med fast label-kolonne, saa alle kontroller
    flugter lodret paa tvaers af sektioner (som i Windows' egne indstillinger).
    """

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle(_("Indstillinger"))
        self.setModal(True)
        self.language_changed = False

        outer = _body(self, margins=(18, 14, 18, 8))
        self._grid = QGridLayout()
        self._grid.setColumnMinimumWidth(0, 210)
        self._grid.setColumnStretch(1, 1)
        self._grid.setVerticalSpacing(theme.SPACE["sm"])
        outer.addLayout(self._grid)
        self._row = 0

        self._build_appearance_section()
        self._build_password_section()
        self._build_language_section()
        self._build_windows_section()
        self._build_updates_section()
        self._build_maintenance_section()
        self._build_log_section()

        outer.addStretch(1)
        outer.addWidget(_separator(self))
        close = QPushButton(_("Luk"), self)
        close.setDefault(True)
        close.clicked.connect(self.accept)
        outer.addWidget(button_row(self, close))

    # -- layout-hjaelpere ------------------------------------------------
    def _section(self, title: str) -> None:
        if self._row:
            spacer = QWidget(self)
            spacer.setFixedHeight(theme.SPACE["lg"])
            self._grid.addWidget(spacer, self._row, 0, 1, 2)
            self._row += 1
        lbl = QLabel(title, self)
        lbl.setFont(theme.font("strong"))
        self._grid.addWidget(lbl, self._row, 0, 1, 2)
        self._row += 1
        self._grid.addWidget(_separator(self), self._row, 0, 1, 2)
        self._row += 1

    def _field(self, label: str, widget: QWidget) -> None:
        self._grid.addWidget(QLabel(label, self), self._row, 0)
        self._grid.addWidget(widget, self._row, 1, Qt.AlignmentFlag.AlignLeft)
        self._row += 1

    def _full(self, widget: QWidget) -> None:
        self._grid.addWidget(widget, self._row, 0, 1, 2)
        self._row += 1

    def _hint(self, text: str) -> None:
        lbl = muted_label(text, parent=self)
        lbl.setWordWrap(True)
        lbl.setMaximumWidth(430)
        self._full(lbl)

    # -- sektioner -------------------------------------------------------
    def _build_appearance_section(self) -> None:
        self._section(_("Udseende"))
        self._theme = QComboBox(self)
        # (config-vaerdi, synlig tekst) -- vaerdien maa ALDRIG vaere den
        # oversatte tekst, ellers laeser en anden sprogversion den ikke.
        self._theme_values = ("system", "light", "dark")
        for label in (_("Følg systemet"), _("Lys"), _("Mørk")):
            self._theme.addItem(label)
        current = self.app.config.get("General", "theme", fallback="system")
        if current not in self._theme_values:
            current = "system"
        self._theme.setCurrentIndex(self._theme_values.index(current))
        self._theme.setMinimumWidth(200)
        self._theme.currentIndexChanged.connect(self._on_theme)
        self._field(_("Tema:"), self._theme)
        self._hint(_("Skiftet slår igennem med det samme."))

    def _on_theme(self, index: int) -> None:
        mode = self._theme_values[max(0, min(index, len(self._theme_values) - 1))]
        self.app.config.set("General", "theme", mode)
        self.app.config.save()
        from PySide6.QtWidgets import QApplication
        theme.set_color_scheme(QApplication.instance(), mode)

    def _build_password_section(self) -> None:
        self._section(_("Kodeordsgætning"))
        self._max_len = QSpinBox(self)
        self._max_len.setRange(1, 10)
        self._max_len.setValue(
            self.app.config.getint("Security", "bruteforce_max_len", fallback=5))
        self._field(_("Maksimal længde at gætte:"), self._max_len)
        self._hint(_("Antal cifre i numeriske koder brute-force forsøger."))

    def _build_language_section(self) -> None:
        self._section(_("Sprog"))
        self._lang = QComboBox(self)
        self._lang.addItems(LocalizationManager.get_supported_languages())
        self._lang.setCurrentText(
            LocalizationManager.get_language_name(self.app.language_code))
        self._lang.setMinimumWidth(200)
        self._lang.currentTextChanged.connect(self._on_language)
        self._field(_("Sprog:"), self._lang)
        self._hint(_("Sprogændringer træder i kraft, når du lukker vinduet."))

    def _on_language(self, name: str) -> None:
        code = LocalizationManager.get_language_code(name)
        if code == self.app.language_code:
            self.language_changed = False
            return
        self.app.config.set("General", "language", code)
        self.app.config.save()
        self.language_changed = True

    def _build_windows_section(self) -> None:
        self._section(_("Windows-integration"))
        self._ctx = QCheckBox(
            _("Tilføj 'Flet med UniteDocs' til højreklik-menuen for PDF-filer"), self)
        self._ctx.setChecked(context_menu.is_registered())
        self._ctx.toggled.connect(self._toggle_context_menu)
        self._full(self._ctx)

    def _toggle_context_menu(self, on: bool) -> None:
        try:
            context_menu.register() if on else context_menu.unregister()
        except Exception as e:
            qt_util.error(self, _("Fejl"), str(e))
            # Rul afkrydsningen tilbage uden at udloese handleren igen.
            self._ctx.blockSignals(True)
            self._ctx.setChecked(not on)
            self._ctx.blockSignals(False)

    def _build_updates_section(self) -> None:
        self._section(_("Opdateringer"))
        auto = QCheckBox(_("Søg automatisk efter opdateringer (én gang om ugen)"), self)
        auto.setChecked(self.app.config.getboolean("Updates", "auto_check", fallback=True))

        def toggled(on: bool) -> None:
            # Skrives med det samme (som sprogvalget), ikke ved lukning -- saa
            # indstillingen overlever ogsaa en haard afslutning.
            self.app.config.set("Updates", "auto_check", "1" if on else "0")
            self.app.config.save()

        auto.toggled.connect(toggled)
        self._full(auto)
        self._hint(_("Unite Docs kontakter www.uniteapps.dk og spørger altid, "
                     "før noget hentes eller installeres."))

        btn = QPushButton(_("Søg efter opdateringer"), self)
        btn.clicked.connect(lambda: self.app.start_update_check(manual=True))
        self._field(_("Manuel kontrol:"), btn)

        skipped = (self.app.config.get("Updates", "skipped_version", fallback="") or "").strip()
        if skipped:
            # Uden denne udvej er "spring denne version over" en enkeltrettet dør.
            link = LinkLabel(_("Nulstil oversprunget version (%s)") % skipped, self)

            def reset() -> None:
                self.app.config.set("Updates", "skipped_version", "")
                self.app.config.save()
                link.set_inert(_("Oversprunget version er nulstillet."))

            link.clicked.connect(reset)
            self._full(link)

    def _build_maintenance_section(self) -> None:
        self._section(_("Vedligeholdelse"))
        btn = QPushButton(_("Slet gemte kodeord"), self)
        btn.clicked.connect(self.app.clear_password_cache)
        self._field(_("Gemte kodeord:"), btn)

    def _build_log_section(self) -> None:
        self._section(_("Log"))
        self._hint(_("Logfilen kan hjælpe med fejlfinding."))
        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(theme.SPACE["xl"])
        dl = LinkLabel(_("Download log"), row)
        dl.clicked.connect(lambda: self.app.download_log(self))
        mail = LinkLabel(_("Send log via e-mail"), row)
        mail.clicked.connect(lambda: self.app.email_log(self))
        rl.addWidget(dl)
        rl.addWidget(mail)
        rl.addStretch(1)
        self._full(row)

    # -- luk -------------------------------------------------------------
    def accept(self) -> None:
        self._save_max_len()
        super().accept()

    def reject(self) -> None:      # Escape lukker med samme semantik som Luk
        self._save_max_len()
        super().accept()

    def _save_max_len(self) -> None:
        v = max(1, min(10, self._max_len.value()))
        self.app.config.set("Security", "bruteforce_max_len", str(v))
        self.app.config.save()


# ==========================================================================
# Fortryd-historik
# ==========================================================================
class HistoryPanel(QWidget):
    """Klikbar tidslinje over fortryd-stakken.

    Modellen har allerede navngivne kommandoer, saa listen er blot dem i
    raekkefoelge: oeverst udgangspunktet, derefter hvert trin der er kørt.
    Trinene UNDER den aktuelle position er gentagelige (de er fortrudt) og vises
    daempet. Et klik springer direkte til den tilstand -- det er dét der goer
    panelet bedre end at trykke Ctrl+Z tolv gange og haabe.

    ``Qt.Popup`` giver den rigtige adfaerd gratis: den lukker ved et klik
    udenfor og staeler ikke fokus fra hovedvinduet.
    """

    seek = Signal(int)          # oensket antal anvendte kommandoer

    def __init__(self, parent, undo_labels, redo_labels):
        super().__init__(parent, Qt.WindowType.Popup)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._applied = len(undo_labels)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(1, 1, 1, 1)
        lay.setSpacing(0)
        frame = QFrame(self)
        frame.setFrameShape(QFrame.Shape.StyledPanel)
        fl = QVBoxLayout(frame)
        fl.setContentsMargins(theme.SPACE["xs"], theme.SPACE["xs"],
                              theme.SPACE["xs"], theme.SPACE["xs"])
        fl.setSpacing(theme.SPACE["xs"])
        lay.addWidget(frame)

        head = QLabel(_("Historik"), frame)
        head.setFont(theme.font("strong"))
        fl.addWidget(head)

        self._list = QListWidget(frame)
        self._list.setObjectName("HistoryList")
        self._list.setFrameShape(QFrame.Shape.NoFrame)
        self._list.setMinimumWidth(240)
        self._list.setMaximumHeight(320)

        # Tidslinje ældst -> nyest. ``undo_labels`` er nyeste-foerst, saa den
        # vendes; ``redo_labels`` er allerede i den raekkefoelge de gentages.
        rows = [_("Udgangspunkt")] + list(reversed(undo_labels)) + list(redo_labels)
        for i, label in enumerate(rows):
            item = QListWidgetItem(_(label) if i else label)
            item.setData(Qt.ItemDataRole.UserRole, i)
            if i > self._applied:
                # Fortrudte trin: stadig klikbare, men tydeligt "foran" os.
                item.setForeground(QColor(theme.C["text_disabled"]))
            self._list.addItem(item)
        self._list.setCurrentRow(self._applied)
        self._list.itemClicked.connect(self._on_click)
        fl.addWidget(self._list)

        hint = muted_label(_("Klik på et trin for at springe dertil."), parent=frame)
        hint.setWordWrap(True)
        fl.addWidget(hint)

    def _on_click(self, item) -> None:
        self.seek.emit(int(item.data(Qt.ItemDataRole.UserRole)))
        self.close()

    def popup_under(self, widget: QWidget) -> None:
        pos = widget.mapToGlobal(widget.rect().bottomLeft())
        self.adjustSize()
        self.move(pos.x(), pos.y() + 2)
        self.show()
        self._list.setFocus()


# ==========================================================================
# Automatisk anonymisering
# ==========================================================================

#: Visningsnavn pr. entitetsnoegle. Noeglen selv oversaettes ALDRIG -- den
#: persisteres i ``AnnotationSpec.label`` og bruges som logiknoegle.
def _entity_labels() -> dict:
    return {
        pii.CPR: _("CPR-nummer"),
        pii.CPR_LOOSE: _("Muligt CPR-nummer"),
        pii.CVR: _("CVR-nummer"),
        pii.PHONE: _("Telefonnummer"),
        pii.ACCOUNT: _("Kontonummer"),
        pii.ADDRESS: _("Adresse"),
        pii.POSTAL: _("Postnummer og by"),
        pii.CASE_NO: _("Sagsnummer"),
        pii.PERSON: _("Navn"),
        pii.LOCATION: _("Sted"),
        pii.ORGANIZATION: _("Organisation"),
        pii.EMAIL: _("E-mailadresse"),
        pii.IBAN: _("IBAN"),
        pii.CREDIT_CARD: _("Betalingskort"),
    }


class AnonymizeOptionsDialog(QDialog):
    """Omfang for en anonymiseringskoersel.

    ``run()`` giver ``{"scope": "all"|"new", "ocr": bool}`` eller ``None``.
    Omfangsvalget vises **kun** naar der allerede er scannede sider -- ellers er
    der intet at vaelge imellem, og dialogen skal ikke staa i vejen.
    """

    def __init__(self, parent, *, total_pages: int, new_pages: int):
        super().__init__(parent)
        self.setWindowTitle(_("Automatisk anonymisering"))
        self.setModal(True)
        lay = _body(self)

        head = QLabel(_("Hvad skal gennemgås?"), self)
        head.setFont(theme.font("heading"))
        lay.addWidget(head)

        # Radioknapperne oprettes KUN naar de ogsaa laegges i layoutet. Blev de
        # bygget med ``self`` som foraelder og derefter sprunget over, placerede
        # Qt dem paa (0, 0) og malede dem oven i overskriften -- teksten loeb
        # bogstaveligt oven i sig selv. En widget med foraelder men uden
        # layout-plads forsvinder ikke; den lander i hjoernet.
        self._has_scope = new_pages < total_pages
        self._only_new = self._all = None
        if self._has_scope:
            self._only_new = QRadioButton(_("Kun nye sider (%d)") % new_pages, self)
            self._all = QRadioButton(_("Alle sider forfra (%d)") % total_pages, self)
            self._only_new.setChecked(True)
            lay.addWidget(self._only_new)
            lay.addWidget(self._all)
            note = muted_label(
                _("Tidligere fravalg huskes og kan slås til igen i listen."),
                parent=self)
            note.setWordWrap(True)
            lay.addWidget(note)

        self._ocr = QCheckBox(_("Læs også scannede sider (tekstgenkendelse)"), self)
        self._ocr.setChecked(True)
        lay.addWidget(self._ocr)
        ocr_note = muted_label(
            _("Scannede sider tager længere tid, men findes ellers slet ikke."),
            parent=self)
        ocr_note.setWordWrap(True)
        lay.addWidget(ocr_note)

        cancel = QPushButton(_("Annuller"), self)
        start = QPushButton(_("Start"), self)
        start.setDefault(True)
        cancel.clicked.connect(self.reject)
        start.clicked.connect(self._start)
        lay.addWidget(button_row(self, cancel, start))
        self._result = None

    def _start(self) -> None:
        scope = "all" if (not self._has_scope or self._all.isChecked()) else "new"
        self._result = {"scope": scope, "ocr": self._ocr.isChecked()}
        self.accept()

    def run(self):
        self.exec()
        return self._result


class AnonymizeReviewDialog(QDialog):
    """Gennemgang af fundne personoplysninger foer de maskeres.

    Traeet har tre niveauer:

    1. **entitetstype** (CPR-nummer, Navn, ...) — tristate over sine grupper
    2. **vaerdigruppe** — én raekke pr. normaliseret vaerdi, saa et fravalg af
       advokatens navn slaar igennem paa *alle* sider paa én gang
    3. **forekomst** — bygges foerst naar gruppen foldes ud, og kun naar den har
       mere end én forekomst

    Alt er afkrydset naar dialogen aabner. Rolle-kolonnen er **vejledende**: den
    fortaeller hvem navnet stod ved siden af (advokat, dommer, tiltalte), saa
    brugeren kan se hvilke navne der kraever en beslutning — men den fravaelger
    aldrig noget af sig selv.
    """

    _ROLE_COL, _COUNT_COL, _CTX_COL = 2, 3, 4

    def __init__(self, parent, groups, result, *, preselected=None,
                 remembered=(), title=None, intro=None, ok_label=None,
                 counter_fmt=None):
        """``title``/``intro``/``ok_label``/``counter_fmt`` findes fordi
        pseudonymiseringen genbruger dialogen uaendret: traeet, filteret og
        afkrydsningslogikken er den samme -- kun ordene om hvad der SKER med de
        valgte er forskellige (maskeres kontra byttes ud)."""
        super().__init__(parent)
        self.setWindowTitle(title or _("Automatisk anonymisering"))
        self.setModal(True)
        self.resize(940, 620)
        self._groups = list(groups)
        self._labels = _entity_labels()
        self._selected = set(preselected) if preselected is not None else None
        self._syncing = False
        self._result = None
        self._intro = intro
        self._counter_fmt = counter_fmt or _("%(valgt)d af %(alle)d forekomster maskeres")

        lay = _body(self)
        lay.addWidget(self._build_header(result, remembered))
        lay.addWidget(self._build_filter())
        self._tree = self._build_tree()
        lay.addWidget(self._tree, 1)

        self._counter = muted_label("", parent=self)
        all_btn = QPushButton(_("Vælg alle"), self)
        none_btn = QPushButton(_("Fravælg alle"), self)
        all_btn.clicked.connect(lambda: self._set_all(True))
        none_btn.clicked.connect(lambda: self._set_all(False))
        foot = QWidget(self)
        fl = QHBoxLayout(foot)
        fl.setContentsMargins(0, 0, 0, 0)
        fl.addWidget(self._counter, 1)
        fl.addWidget(all_btn)
        fl.addWidget(none_btn)
        lay.addWidget(foot)

        cancel = QPushButton(_("Annuller"), self)
        ok = QPushButton(ok_label or _("Masker de valgte"), self)
        ok.setDefault(True)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self._accept)
        lay.addWidget(button_row(self, cancel, ok))
        self._update_counter()

    # -- opbygning ---------------------------------------------------------
    def _build_header(self, result, remembered) -> QWidget:
        box = QWidget(self)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(0, 0, 0, 0)
        bl.setSpacing(theme.SPACE["xs"])

        total = sum(g.count for g in self._groups)
        head = QLabel(_("%(fund)d forekomster fordelt på %(grupper)d værdier")
                      % {"fund": total, "grupper": len(self._groups)}, box)
        head.setFont(theme.font("heading"))
        bl.addWidget(head)

        sub = muted_label(
            self._intro or
            _("Alt er valgt. Fjern fluebenet ved det der IKKE skal maskeres — "
              "fx advokatens eller dommerens navn."), parent=box)
        sub.setWordWrap(True)
        bl.addWidget(sub)

        if getattr(result, "pages_ocred", 0):
            bl.addWidget(self._notice(
                box, "warning",
                _("%d side(r) blev læst med tekstgenkendelse. Kontrollér "
                  "resultatet — genkendelsen kan have overset tekst.")
                % result.pages_ocred))
        if getattr(result, "pages_english", 0):
            bl.addWidget(self._notice(
                box, "muted",
                _("%d side(r) er læst som engelsk tekst ud over dansk.")
                % result.pages_english))
        if getattr(result, "pages_english_missing", 0):
            # Den danske model alene finder maalt 12 af 18 navne i engelsk tekst.
            # Det er undermaskering, og brugeren skal vide det.
            bl.addWidget(self._notice(
                box, "warning",
                _("%d side(r) ser engelske ud, men den engelske sprogmodel er "
                  "ikke installeret. Numre og adresser er fundet, men engelske "
                  "navne kan være overset.") % result.pages_english_missing))
        if getattr(result, "pages_failed", 0):
            bl.addWidget(self._notice(
                box, "danger",
                _("%d side(r) kunne ikke læses.") % result.pages_failed))
        if getattr(result, "cancelled", False):
            bl.addWidget(self._notice(
                box, "warning",
                _("Gennemgangen blev afbrudt — listen er ikke komplet.")))
        if remembered:
            bl.addWidget(self._notice(
                box, "muted",
                _("Husket fra sidste gennemgang, forud-fravalgt: %s")
                % ", ".join(remembered[:6])))
        return box

    def _notice(self, parent, kind: str, text: str) -> QLabel:
        lab = QLabel(text, parent)
        lab.setWordWrap(True)
        color = theme.C["text_muted"] if kind == "muted" else theme.C[kind]
        lab.setStyleSheet("color: %s;" % color)
        return lab

    def _build_filter(self) -> QWidget:
        row = QWidget(self)
        rl = QHBoxLayout(row)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(QLabel(_("Filtrér:"), row))
        self._filter = QLineEdit(row)
        self._filter.setPlaceholderText(_("Skriv for at søge i værdier og tekst…"))
        self._filter.textChanged.connect(self._apply_filter)
        rl.addWidget(self._filter, 1)
        return row

    def _build_tree(self) -> QTreeWidget:
        tree = QTreeWidget(self)
        tree.setColumnCount(5)
        tree.setHeaderLabels([_("Værdi"), _("Type"), _("Rolle"), _("Antal"),
                              _("Eksempel")])
        tree.setUniformRowHeights(True)
        tree.setAlternatingRowColors(True)
        tree.setExpandsOnDoubleClick(False)
        tree.itemChanged.connect(self._on_item_changed)
        tree.itemExpanded.connect(self._on_expanded)
        tree.itemDoubleClicked.connect(self._on_double_click)

        by_entity = {}
        for g in self._groups:
            by_entity.setdefault(g.entity, []).append(g)

        self._syncing = True
        for entity, groups in by_entity.items():
            top = QTreeWidgetItem(tree)
            top.setText(0, self._labels.get(entity, entity))
            top.setText(self._COUNT_COL, str(sum(g.count for g in groups)))
            top.setFont(0, theme.font("bold"))
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable
                         | Qt.ItemFlag.ItemIsAutoTristate)
            top.setData(0, Qt.ItemDataRole.UserRole, ("entity", entity))
            for g in groups:
                self._add_group(top, g)
            top.setExpanded(True)
        self._syncing = False

        tree.setColumnWidth(0, 250)
        tree.setColumnWidth(1, 130)
        tree.setColumnWidth(2, 90)
        tree.setColumnWidth(3, 60)
        return tree

    def _add_group(self, parent, g) -> None:
        item = QTreeWidgetItem(parent)
        item.setText(0, g.display)
        item.setText(1, self._labels.get(g.entity, g.entity))
        item.setText(self._ROLE_COL, self._role_text(g.role_hint))
        item.setText(self._COUNT_COL, str(g.count))
        item.setText(self._CTX_COL, g.findings[0].context if g.findings else "")
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        if g.count > 1:
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsAutoTristate)
        item.setData(0, Qt.ItemDataRole.UserRole, ("group", g))
        state = self._group_state(g)
        # Pladsholderen SKAL indsaettes foer afkrydsningen saettes, og den skal
        # selv vaere afkrydselig: med ``ItemIsAutoTristate`` udleder Qt foraeldrens
        # tilstand af boernene, saa et ikke-afkryds­eligt barn indsat bagefter
        # nulstiller gruppen til "fravalgt".
        if g.count > 1:
            self._add_placeholder(item, state)
        item.setCheckState(0, state)
        if g.role_hint:
            item.setForeground(self._ROLE_COL, QColor(theme.C["accent"]))
        # Er forekomsterne uenige om rollen, skal brugeren se splittet med det
        # samme — det er praecis dér en beslutning er noedvendig.
        if g.count > 1 and g.role_hint == anonymize.ROLE_MIXED:
            item.setExpanded(True)

    def _add_placeholder(self, item, state) -> None:
        holder = QTreeWidgetItem(item)
        holder.setText(0, _("Indlæser…"))
        holder.setData(0, Qt.ItemDataRole.UserRole, ("placeholder", None))
        holder.setFlags(Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsUserCheckable)
        holder.setCheckState(0, Qt.CheckState.Checked
                             if state != Qt.CheckState.Unchecked
                             else Qt.CheckState.Unchecked)

    def _role_text(self, hint: str) -> str:
        if not hint:
            return ""
        if hint == anonymize.ROLE_MIXED:
            return _("blandet")
        return hint

    def _group_state(self, g):
        if self._selected is None:
            return Qt.CheckState.Checked
        on = sum(1 for f in g.findings if f.uid in self._selected)
        if on == 0:
            return Qt.CheckState.Unchecked
        if on == len(g.findings):
            return Qt.CheckState.Checked
        return Qt.CheckState.PartiallyChecked

    # -- doven udfoldning --------------------------------------------------
    def _on_expanded(self, item) -> None:
        """Byg forekomstraekkerne foerst naar gruppen aabnes.

        Et bundt kan have tusindvis af forekomster; at bygge dem alle paa
        forhaand ville goere dialogen sekunder om at aabne.
        """
        if item.childCount() != 1:
            return
        payload = item.child(0).data(0, Qt.ItemDataRole.UserRole)
        if not payload or payload[0] != "placeholder":
            return
        role = item.data(0, Qt.ItemDataRole.UserRole)
        if not role or role[0] != "group":
            return
        g = role[1]
        group_state = item.checkState(0)

        self._syncing = True
        item.takeChild(0)
        for f in g.findings:
            child = QTreeWidgetItem(item)
            child.setText(0, f.page_label)
            child.setText(1, self._labels.get(f.entity, f.entity))
            child.setText(self._ROLE_COL, self._role_text(f.role_hint))
            child.setText(self._CTX_COL, f.context)
            child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            child.setData(0, Qt.ItemDataRole.UserRole, ("finding", f))
            if group_state == Qt.CheckState.Checked:
                on = True
            elif group_state == Qt.CheckState.Unchecked:
                on = False
            else:
                on = self._selected is None or f.uid in self._selected
            child.setCheckState(0, Qt.CheckState.Checked if on
                                else Qt.CheckState.Unchecked)
        self._syncing = False

    def _on_double_click(self, item, _column) -> None:
        """Dobbeltklik paa en forekomst: spring til siden i sidevisningen."""
        role = item.data(0, Qt.ItemDataRole.UserRole)
        if not role or role[0] != "finding":
            item.setExpanded(not item.isExpanded())
            return
        canvas = getattr(getattr(self.parent(), "page_view", None), "pcanvas", None)
        if canvas is not None:
            try:
                canvas.goto_page(role[1].page_uid)
            except Exception:
                logger.debug("Kunne ikke springe til siden", exc_info=True)

    # -- afkrydsning -------------------------------------------------------
    def _on_item_changed(self, _item, _column) -> None:
        # Qt propagerer selv tristate op og ned; uden vagten ville vores egne
        # programmatiske aendringer rekursere gennem den propagering.
        if self._syncing:
            return
        self._update_counter()

    def _set_all(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self._syncing = True
        for i in range(self._tree.topLevelItemCount()):
            self._tree.topLevelItem(i).setCheckState(0, state)
        self._syncing = False
        self._update_counter()

    def _walk_groups(self):
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            for j in range(top.childCount()):
                item = top.child(j)
                role = item.data(0, Qt.ItemDataRole.UserRole)
                if role and role[0] == "group":
                    yield item, role[1]

    def selected_uids(self) -> set:
        """Fund-uid'er brugeren har ladet staa afkrydset."""
        out = set()
        for item, g in self._walk_groups():
            state = item.checkState(0)
            if state == Qt.CheckState.Checked:
                out.update(f.uid for f in g.findings)
            elif state == Qt.CheckState.PartiallyChecked:
                seen = False
                for k in range(item.childCount()):
                    child = item.child(k)
                    role = child.data(0, Qt.ItemDataRole.UserRole)
                    if not role or role[0] != "finding":
                        continue
                    seen = True
                    if child.checkState(0) == Qt.CheckState.Checked:
                        out.add(role[1].uid)
                if not seen and self._selected is not None:
                    # Gruppen er delvist valgt, men aldrig foldet ud, saa der er
                    # ingen forekomstraekker at laese. Forvalget er stadig facit.
                    out.update(f.uid for f in g.findings
                               if f.uid in self._selected)
        return out

    def _update_counter(self) -> None:
        total = sum(g.count for g in self._groups)
        self._counter.setText(self._counter_fmt
                              % {"valgt": len(self.selected_uids()),
                                 "alle": total})

    # -- filter ------------------------------------------------------------
    def _apply_filter(self, text: str) -> None:
        needle = (text or "").strip().casefold()
        for i in range(self._tree.topLevelItemCount()):
            top = self._tree.topLevelItem(i)
            any_visible = False
            for j in range(top.childCount()):
                item = top.child(j)
                role = item.data(0, Qt.ItemDataRole.UserRole)
                if not role or role[0] != "group":
                    continue
                g = role[1]
                hit = (not needle or needle in g.display.casefold()
                       or any(needle in f.context.casefold() for f in g.findings))
                item.setHidden(not hit)
                any_visible = any_visible or hit
            top.setHidden(not any_visible)

    # -- afslutning --------------------------------------------------------
    def _accept(self) -> None:
        self._result = self.selected_uids()
        self.accept()

    def run(self):
        self.exec()
        return self._result


# ==========================================================================
# Kopiér tekst til udklipsholderen
# ==========================================================================
#: Sektionen i ``config.ini`` hvor valgene huskes fra gang til gang.
_CLIP_SECTION = "Clipboard"


class ClipboardOptionsDialog(QDialog):
    """Hvad der skal ske foer teksten lander i udklipsholderen.

    ``run()`` giver ``{"pseudonymize": bool, "review": bool}`` eller ``None``.

    Begge valg **huskes** i ``config.ini``. Det er bevidst: den der kopierer
    tekst til en sprogmodel goer det mange gange i traek og skal ikke tage
    stilling forfra hver gang. Standardvaerdien for et ubeskrevet blad er
    "pseudonymisér" -- det sikre valg.
    """

    def __init__(self, parent, *, page_count: int, pii_available: bool = True):
        super().__init__(parent)
        self.setWindowTitle(_("Kopiér til udklipsholder"))
        self.setModal(True)
        lay = _body(self)
        cfg = AppConfig()

        head = QLabel(_("Kopiér tekst fra %d side(r)") % page_count, self)
        head.setFont(theme.font("heading"))
        lay.addWidget(head)

        self._pseudo = QCheckBox(
            _("Pseudonymisér personoplysninger først"), self)
        self._pseudo.setChecked(
            cfg.getboolean(_CLIP_SECTION, "pseudonymize", fallback=True)
            and pii_available)
        self._pseudo.setEnabled(pii_available)
        lay.addWidget(self._pseudo)

        note = muted_label(
            _("Skal teksten uploades til en AI-tjeneste, bør du gøre det. "
              "Navne, CPR-numre og adresser byttes ud med etiketter som "
              "«Person 1». Ombytningen kan ikke fortrydes, og den REDUCERER "
              "— den fjerner ikke — risikoen for at nogen kan genkendes."),
            parent=self)
        note.setWordWrap(True)
        lay.addWidget(note)

        if not pii_available:
            missing = QLabel(
                _("Sprogmodellen er ikke installeret, så teksten kan kun "
                  "kopieres som den er."), self)
            missing.setWordWrap(True)
            missing.setStyleSheet("color: %s;" % theme.C["warning"])
            lay.addWidget(missing)

        self._review = QCheckBox(_("Gennemgå fundene før kopiering"), self)
        self._review.setChecked(
            cfg.getboolean(_CLIP_SECTION, "review", fallback=False))
        self._review.setEnabled(self._pseudo.isChecked())
        self._pseudo.toggled.connect(self._review.setEnabled)
        lay.addWidget(self._review)

        review_note = muted_label(
            _("Så kan du fravælge fx din egen klients navn, inden teksten "
              "byttes om."), parent=self)
        review_note.setWordWrap(True)
        lay.addWidget(review_note)

        ocr_note = muted_label(
            _("Sider uden tekstlag læses automatisk med tekstgenkendelse."),
            parent=self)
        ocr_note.setWordWrap(True)
        lay.addWidget(ocr_note)

        cancel = QPushButton(_("Annuller"), self)
        start = QPushButton(_("Kopiér"), self)
        start.setDefault(True)
        cancel.clicked.connect(self.reject)
        start.clicked.connect(self._start)
        lay.addWidget(button_row(self, cancel, start))
        self._result = None

    def _start(self) -> None:
        self._result = {"pseudonymize": self._pseudo.isChecked(),
                        "review": (self._pseudo.isChecked()
                                   and self._review.isChecked())}
        cfg = AppConfig()
        cfg.set(_CLIP_SECTION, "pseudonymize", str(self._pseudo.isChecked()))
        cfg.set(_CLIP_SECTION, "review", str(self._review.isChecked()))
        try:
            cfg.save()
        except OSError as e:
            # At kunne kopiere er vigtigere end at kunne huske valget.
            logger.warning("Kunne ikke gemme udklipsholder-valget: %s", e)
        self.accept()

    def run(self):
        self.exec()
        return self._result


class ClipboardDoneDialog(QDialog):
    """Kvittering: hvad der blev kopieret, og vejen til navneoversigten."""

    def __init__(self, parent, *, chars: int, pages: int, pseudonyms: int,
                 pseudonymized: bool, ocred: int = 0, failed: int = 0,
                 save_overview=None):
        super().__init__(parent)
        self.setWindowTitle(_("Kopiér til udklipsholder"))
        self.setModal(True)
        lay = _body(self)

        head = QLabel(_("Teksten er kopieret"), self)
        head.setFont(theme.font("heading"))
        lay.addWidget(head)

        lay.addWidget(muted_label(
            _("%(tegn)s tegn fra %(sider)d side(r).")
            % {"tegn": "{:,}".format(chars).replace(",", "."), "sider": pages},
            parent=self))

        if pseudonymized:
            if pseudonyms:
                lay.addWidget(QLabel(
                    _("%d personoplysning(er) er byttet ud med pseudonymer.")
                    % pseudonyms, self))
            else:
                lay.addWidget(QLabel(
                    _("Der blev ikke fundet nogen personoplysninger at bytte ud."),
                    self))
        else:
            warn = QLabel(_("Teksten er IKKE pseudonymiseret."), self)
            warn.setStyleSheet("color: %s;" % theme.C["warning"])
            lay.addWidget(warn)

        if ocred:
            note = QLabel(
                _("%d side(r) blev læst med tekstgenkendelse. Kontrollér "
                  "resultatet — genkendelsen kan have overset tekst.") % ocred,
                self)
            note.setWordWrap(True)
            note.setStyleSheet("color: %s;" % theme.C["warning"])
            lay.addWidget(note)
        if failed:
            bad = QLabel(_("%d side(r) kunne ikke læses.") % failed, self)
            bad.setStyleSheet("color: %s;" % theme.C["danger"])
            lay.addWidget(bad)

        buttons = []
        if pseudonymized and pseudonyms and save_overview is not None:
            overview = QPushButton(_("Gem navneoversigt…"), self)
            overview.clicked.connect(lambda: save_overview())
            buttons.append(overview)
            hint = muted_label(
                _("Oversigten kobler pseudonym og original. Den må ikke "
                  "uploades sammen med teksten."), parent=self)
            hint.setWordWrap(True)
            lay.addWidget(hint)

        close = QPushButton(_("Luk"), self)
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons.append(close)
        lay.addWidget(button_row(self, *buttons))
