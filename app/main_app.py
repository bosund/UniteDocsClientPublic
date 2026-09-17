"""Hovedvinduet.

``MainWindow`` ejer kommandobaren, statuslinjen, sidevisningen og al
orkestrering af baggrundsarbejde. Den er *ikke* sandhedskilde for dokumentet --
det er ``edit_model.EditModel`` -- og den roerer aldrig PDF-motoren direkte;
alt tungt arbejde ligger i ``save_pipeline``, ``export_formats``,
``password_guesser`` og ``updater``, som alle er UI-frie og kan testes uden et
vindue.

To ting adskiller den fra 8.x' ``PDFTool(tk.Tk)``:

* **Ingen ``after(50)``-pumpe.** Worker-traade leverer stadig gennem
  ``self._queue.put((fn, args))``, men ``_queue`` er nu en
  ``qt_util.MainThreadInvoker``, der sender et Qt-signal til UI-traaden. Et
  resultat vises i det oejeblik det er klart i stedet for op til 50 ms senere.
* **Dialogerne bor i ``dialogs.py``.** Hovedvinduet kalder dem; det bygger dem
  ikke.

Traadreglen fra 8.x staar uaendret: **en worker roerer aldrig en widget.** Alt
Qt-afhaengigt snapshottes paa UI-traaden foer traaden startes (kodeordslister,
job-lister, iid/sti-par), og resultater gaar tilbage gennem ``_queue``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pymupdf
from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QFileDialog, QGridLayout,
                               QHBoxLayout, QLabel, QMainWindow, QSizePolicy,
                               QToolButton, QVBoxLayout, QWidget)

from . import __version__
from . import annotations
from . import anonymize
from . import dialogs
from . import edit_model as em
from . import export_formats
from . import icons_vector
from . import page_render
from . import password_guesser
from . import pdf_renderer
from . import pii
from . import pdf_utils
from . import pseudonymize
from . import qt_util
from . import save_pipeline
from . import theme
from . import updater
from . import utils
from .config import AppConfig
from .localization import LocalizationManager
from .logging_config import get_logger
from .page_view import PageView
from .qt_util import LinkLabel, muted_label
from .tooltip import Tooltip
from .undo_stack import Command, UndoStack

logger = get_logger(__name__)

# INTERNATIONALIZATION: Global translation function
# AI ASSISTANTS: Always use _("text") for user-visible strings
# Example: QLabel(_("Save file"))
# After adding new _("strings"), run: python update_locales.py
_ = LocalizationManager.get_text

_SUPPORTED_DROP_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}


class MainWindow(QMainWindow):

    # Sorterings-panelets raekker: (noegle, label, har_retning)
    _SORT_ROWS = (
        (em.SORT_REVERSE, "Omvendt", False),
        (em.SORT_DATE, "Oprettelsesdato", True),
        (em.SORT_NAME, "Navn", True),
        (em.SORT_SIZE, "Størrelse", True),
    )

    def __init__(self, initial_files: list[str] | None = None):
        super().__init__()
        self.config = AppConfig()
        self.language_code = self.config.get("General", "language", fallback="en")
        LocalizationManager.initialize(self.language_code)

        self.setWindowTitle("Unite Docs – v%s" % __version__)
        self._apply_default_geometry()
        self.setMinimumSize(1000, 560)
        self.setAcceptDrops(True)

        # --- Tilstand -----------------------------------------------------
        # EditModel er eneste sandhed for fil-/side-raekkefoelge og redigering.
        self.model = em.EditModel()
        self.undo_stack = UndoStack()
        self.page_view: PageView | None = None

        # Kodeordslisten er app-tilstand (ikke en widget), saa den overlever et
        # sprogskift hvor hele fladen bygges om.
        self._pw_lines: list[str] = []
        self._pw_cache: list[str] | None = None
        self._pw_dialog: dialogs.PasswordsDialog | None = None
        self._insert_dialog: dialogs.InsertPageDialog | None = None
        self._inserted_dir: Path | None = None
        self._prompt_on_metadata: set[str] = set()
        self._pending_unlock: list[str] = []
        self._unlocking = False
        self._sort_panel: dialogs.SortPanel | None = None
        self._sort_dir = {em.SORT_DATE: False, em.SORT_NAME: False, em.SORT_SIZE: False}
        self._guess_dialog: dialogs.GuessProgressDialog | None = None
        self._history_panel = None
        self._export_progress: qt_util.ProgressDialog | None = None
        self._update_busy = False
        self._closing = False

        # --- Baggrundsarbejde --------------------------------------------
        # ``put((fn, args))`` fra en hvilken som helst traad; leveres paa
        # UI-traaden via et koeet Qt-signal (ingen polling).
        self._queue = qt_util.MainThreadInvoker(self)
        self.page_render_mgr = page_render.PageRenderManager(self)

        utils.purge_old_inserted_dirs()

        # Anonymisering er en SESSION, ikke en engangskoersel: tilstanden
        # her er dét der faar en fil tilfoejet BAGEFTER til at blive fanget.
        self._anon_session = anonymize.AnonymizeSession()
        self._anon_progress_at = 0.0
        self._anon_asking = False

        self._themed_icons: list = []
        self._build_ui()
        self._install_shortcuts()
        theme.on_scheme_changed(self._refresh_themed_icons)
        self.page_render_mgr.start()

        self._cleanup_timer = QTimer(self)
        self._cleanup_timer.timeout.connect(utils.purge_stale_temp_files)
        self._cleanup_timer.start(30_000)

        # Debounce af mange metadata-callbacks til én genopbygning.
        self._pv_refresh_timer = QTimer(self)
        self._pv_refresh_timer.setSingleShot(True)
        self._pv_refresh_timer.timeout.connect(self._ensure_pages_then_rebuild)

        # Forbigaaende statusbeskeder nulstilles af denne.
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(self._refresh_status)

        self._restore_session()
        QTimer.singleShot(0, self._first_paint)
        if initial_files:
            QTimer.singleShot(300, lambda: self._add_paths(list(initial_files)))
        QTimer.singleShot(200, self._start_ipc)
        # Opdateringstjekket skal ikke kappes om CPU'en med foerste optegning,
        # IPC-starten eller indlaesningen af initial_files.
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self.start_update_check)
        self._update_timer.start(8000)

    # ------------------------------------------------------------------
    # Opbygning af fladen
    # ------------------------------------------------------------------
    def _cmd_button(self, icon: str, text: str, slot, *, primary: bool = False,
                    tip=None, shortcut: str | None = None,
                    checkable: bool = False) -> QToolButton:
        """Kommandobar-knap: stort ikon over forklarende tekst.

        Den synlige tekst ER labellen; en tooltip tilfoejes kun naar den baerer
        EKSTRA information (genvej eller tilstand)."""
        btn = QToolButton(self)
        btn.setObjectName("CmdPrimary" if primary else "CmdButton")
        btn.setText(text)
        btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
        size = theme.ICON["cmdlg"]
        color = theme.C["selection_fg"] if primary else None
        btn.setIcon(icons_vector.qicon(icon, size, color))
        btn.setIconSize(QSize(size, size))
        # Ikonet er tegnet i tekstfarven, saa det skal gentegnes ved temaskift.
        self._themed_icons.append((btn, icon, size, primary))
        btn.setCheckable(checkable)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        if tip is not None or shortcut is not None:
            Tooltip.attach(btn, tip if tip is not None else text, shortcut=shortcut)
        return btn

    def _icon_button(self, icon: str, tip: str, slot, *, shortcut=None) -> QToolButton:
        """Kompakt ikon-kun-knap (pil-klyngen)."""
        btn = QToolButton(self)
        btn.setObjectName("ToolIcon")
        size = theme.ICON["small"]
        btn.setIcon(icons_vector.qicon(icon, size))
        btn.setIconSize(QSize(size, size))
        self._themed_icons.append((btn, icon, size, False))
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(slot)
        Tooltip.attach(btn, tip, shortcut=shortcut)
        return btn

    def _refresh_themed_icons(self) -> None:
        """Gentegn hvert ikon i den nye tekstfarve efter et lys/moerk-skift.

        ``QIcon`` cacher sine pixmaps, saa det raekker ikke at rydde
        ikon-cachen -- hver knap skal have et nyt ``QIcon``."""
        icons_vector.clear_qicon_cache()
        live = []
        for btn, name, size, primary in self._themed_icons:
            try:
                color = theme.C["selection_fg"] if primary else None
                btn.setIcon(icons_vector.qicon(name, size, color))
                live.append((btn, name, size, primary))
            except RuntimeError:
                pass          # widget'en er revet ned (sprogskift)
        self._themed_icons = live
        if self.page_view is not None:
            self.page_view.refresh_themed_icons()

    def _build_ui(self) -> None:
        self._themed_icons: list = []
        central = QWidget(self)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_command_bar())
        outer.addWidget(theme.hairline("horizontal", central))

        self.page_view = PageView(central, self)
        outer.addWidget(self.page_view, 1)

        outer.addWidget(theme.hairline("horizontal", central))
        outer.addWidget(self._build_status_bar())
        self.setCentralWidget(central)

        self.undo_stack.subscribe(self._update_undo_buttons)
        self._update_undo_buttons()

    def _build_command_bar(self) -> QWidget:
        """Én flad raekke med store ikoner over forklarende tekst.

        Raekkefoelge venstre->hoejre: Tilfoej filer · Flet og gem · Gem
        enkeltfiler ‖ Venstre · Hoejre · Beskaer ‖ [pil-klynge] · Sorter ·
        Indsaet side · Slet ‖ Fortryd · Gentag ‖ Kodeord -- og hoejrestillet
        Indstillinger (Windows-konvention).
        """
        bar = QWidget(self)
        bar.setObjectName("CommandBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(theme.SPACE["md"], theme.SPACE["sm"],
                               theme.SPACE["md"], theme.SPACE["sm"])
        lay.setSpacing(theme.SPACE["xxs"])

        def sep() -> None:
            line = theme.hairline("vertical", bar)
            line.setFixedHeight(46)
            lay.addSpacing(theme.SPACE["sm"])
            lay.addWidget(line)
            lay.addSpacing(theme.SPACE["sm"])

        # --- Filer ---
        lay.addWidget(self._cmd_button("add", _("Tilføj filer"), self._add,
                                       primary=True, shortcut="Ctrl+O"))
        self._merge_btn = self._cmd_button("save", _("Flet og gem"), self._merge,
                                           primary=True, shortcut="Ctrl+S")
        lay.addWidget(self._merge_btn)
        lay.addWidget(self._cmd_button("decrypt", _("Gem enkeltfiler"), self._save_dec,
                                       shortcut="Ctrl+Shift+S"))
        lay.addWidget(self._cmd_button(
            "clipboard", _("Kopiér tekst"), self._copy_to_clipboard,
            shortcut="Ctrl+Shift+C",
            tip=_("Kopiér dokumentets tekst — med mulighed for at "
                  "pseudonymisere personoplysninger først.")))
        sep()

        # --- Roter / beskaer ---
        lay.addWidget(self._cmd_button("rotate_left", _("Venstre"), self._rotate_left,
                                       shortcut="Ctrl+Left"))
        lay.addWidget(self._cmd_button("rotate_right", _("Højre"), self._rotate_right,
                                       shortcut="Ctrl+Right"))
        self._crop_btn = self._cmd_button(
            "crop", _("Beskær"), self._toggle_crop_tool, checkable=True,
            tip=_("Træk en ramme for at beskære den viste side"))
        lay.addWidget(self._crop_btn)
        sep()

        # --- Tekstgenkendelse og anonymisering ---
        # OCR staar til VENSTRE for Anonymiser: paa en scanning er den
        # forudsaetningen for baade at kunne markere tekst og for at
        # anonymiseringen finder andet end tal.
        lay.addWidget(self._cmd_button(
            "ocr_run", _("Tekstgenkendelse"), self._run_ocr,
            tip=_("Kør tekstgenkendelse på scannede filer, så du kan markere tekst.")))
        self._anon_btn = self._cmd_button(
            "anonymize", _("Anonymiser"), self._start_anonymize,
            tip=lambda: self._anon_tip())
        self._anon_btn.setEnabled(pii.availability()[0])
        lay.addWidget(self._anon_btn)
        sep()

        # --- Raekkefoelge: kompakt 2x2 pil-klynge + sorter + indsaet + slet ---
        order = QWidget(bar)
        grid = QGridLayout(order)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(theme.SPACE["xxs"])
        grid.addWidget(self._icon_button("up", _("Flyt op"), self._up,
                                         shortcut="Alt+Up"), 0, 0)
        grid.addWidget(self._icon_button("down", _("Flyt ned"), self._down,
                                         shortcut="Alt+Down"), 1, 0)
        grid.addWidget(self._icon_button("top", _("Flyt øverst"), self._move_top,
                                         shortcut="Alt+Home"), 0, 1)
        grid.addWidget(self._icon_button("bottom", _("Flyt nederst"), self._move_bottom,
                                         shortcut="Alt+End"), 1, 1)
        lay.addWidget(order)
        lay.addSpacing(theme.SPACE["xs"])

        self._sort_btn = self._cmd_button("sort", _("Sorter"), self._toggle_sort_panel,
                                          tip=_("Sortér filerne"), checkable=True)
        lay.addWidget(self._sort_btn)
        lay.addWidget(self._cmd_button(
            "insert_page", _("Indsæt side"), self._open_insert_page_dialog,
            tip=_("Indsæt en ny side med overskrift og tekst")))
        lay.addWidget(self._cmd_button("delete", _("Slet"), self._delete_selection,
                                       shortcut="Delete"))
        sep()

        # --- Fortryd / gentag ---
        self._undo_btn = self._cmd_button("undo_preview", _("Fortryd"), self._do_undo,
                                          shortcut="Ctrl+Z")
        self._redo_btn = self._cmd_button("redo_preview", _("Gentag"), self._do_redo,
                                          shortcut="Ctrl+Y")
        self._undo_btn.setEnabled(False)
        self._redo_btn.setEnabled(False)
        lay.addWidget(self._undo_btn)
        lay.addWidget(self._redo_btn)
        self._history_btn = self._icon_button(
            "history", _("Historik"), self._toggle_history, shortcut="Ctrl+H")
        self._history_btn.setEnabled(False)
        lay.addWidget(self._history_btn)
        sep()

        # --- Kodeord (tooltip viser antallet) ---
        lay.addWidget(self._cmd_button(
            "key", _("Kodeord"), self._open_passwords_dialog,
            tip=lambda: _("Kodeord (%d gemt)") % len(self._pw_lines)))

        lay.addStretch(1)
        lay.addWidget(self._cmd_button("settings", _("Indstillinger"),
                                       self._open_settings_window))
        return bar

    def _build_status_bar(self) -> QWidget:
        """Nederste linje: venstre = kontekstuel status, hoejre = version + links."""
        bar = QWidget(self)
        bar.setObjectName("StatusBar")
        bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(theme.SPACE["md"], theme.SPACE["xs"],
                               theme.SPACE["md"], theme.SPACE["xs"])
        lay.setSpacing(theme.SPACE["sm"])

        self._status_label = QLabel(self._default_status(), bar)
        self._status_label.setObjectName("Muted")
        self._status_label.setSizePolicy(QSizePolicy.Policy.Expanding,
                                         QSizePolicy.Policy.Preferred)
        lay.addWidget(self._status_label, 1)

        lay.addWidget(muted_label("v%s · 2025 · Bo Sundgaard" % __version__, parent=bar))
        lay.addWidget(muted_label("·", parent=bar))
        lay.addWidget(LinkLabel("www.uniteapps.dk", bar, url="https://www.uniteapps.dk"))
        lay.addWidget(muted_label("·", parent=bar))
        creds = LinkLabel(_("Credits"), bar)
        creds.clicked.connect(self._show_credits)
        lay.addWidget(creds)
        return bar

    def _install_shortcuts(self) -> None:
        """Globale genveje.

        ``Ctrl+Z``/``Ctrl+Y`` er ``WindowShortcut``, saa et tekstfelt med fokus
        beholder sin *egen* fortrydelse -- 8.x maatte tjekke ``focus_get()``
        manuelt for det samme.
        """
        def add(seq: str, slot, context=Qt.ShortcutContext.WindowShortcut) -> None:
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(context)
            sc.activated.connect(slot)

        add("Ctrl+O", self._add)
        add("Ctrl+S", self._merge)
        add("Ctrl+Shift+S", self._save_dec)
        add("Ctrl+Shift+C", self._copy_to_clipboard)
        add("Ctrl+Z", self._do_undo)
        add("Ctrl+Y", self._do_redo)
        add("Ctrl+Shift+Z", self._do_redo)
        add("Ctrl+Left", self._rotate_left)
        add("Ctrl+Right", self._rotate_right)
        add("Alt+Up", self._up)
        add("Alt+Down", self._down)
        add("Alt+Home", self._move_top)
        add("Alt+End", self._move_bottom)
        add("Ctrl+H", self._toggle_history)
        add("Esc", self._on_escape)
        add("F1", self._show_credits)

    # ------------------------------------------------------------------
    # Session: vinduets stoerrelse, panelernes fordeling, zoom
    # ------------------------------------------------------------------
    _SESSION = "Window"
    # Mindste klientflade hvor kommandobarens knaptekster staar helt (maalt paa
    # en 100 %-skaerm). Qt regner i logiske pixels, saa skalering er daekket.
    _DEFAULT_SIZE = (1510, 1025)
    # Titellinje + kant, som ``resize()`` ikke medregner.
    _FRAME_ALLOWANCE = (16, 48)
    # Geometri gemt foer 1510x1025 blev standard er den gamle, for smalle
    # 1200x820 -- den genskabes ikke. Hæv tallet hvis standarden skifter igen.
    _GEOMETRY_VERSION = 2

    def _apply_default_geometry(self) -> None:
        """Standardstørrelse, skaaret til skaermens frie areal og centreret."""
        w, h = self._DEFAULT_SIZE
        screen = QApplication.primaryScreen()
        if screen is None:
            self.resize(w, h)
            return
        avail = screen.availableGeometry()
        w = min(w, avail.width() - self._FRAME_ALLOWANCE[0])
        h = min(h, avail.height() - self._FRAME_ALLOWANCE[1])
        self.resize(w, h)
        self.move(avail.x() + (avail.width() - w) // 2,
                  avail.y() + max(0, (avail.height() - h - self._FRAME_ALLOWANCE[1]) // 2))

    def _restore_session(self) -> None:
        """Genskab sidste kørsels vindue og paneler.

        Alt er valgfrit og pakket ind: en beskadiget eller forældet vaerdi maa
        aldrig forhindre opstart -- saa faar man blot standardlayoutet."""
        cfg = self.config
        try:
            geo = cfg.get(self._SESSION, "geometry", fallback="")
            version = cfg.getint(self._SESSION, "geometry_version", fallback=0)
            if geo and version >= self._GEOMETRY_VERSION:
                from PySide6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromBase64(geo.encode("ascii")))
        except Exception as e:
            logger.debug("Kunne ikke genskabe vinduesgeometri: %s", e)
        try:
            sizes = [int(v) for v in
                     (cfg.get(self._SESSION, "splitter", fallback="") or "").split(",")
                     if v.strip()]
            if len(sizes) == 3 and sum(sizes) > 0:
                self.page_view.splitter.setSizes(sizes)
                self.page_view.set_file_list_visible(sizes[0] > 20)
        except Exception as e:
            logger.debug("Kunne ikke genskabe panelbredder: %s", e)
        try:
            self.page_view.set_tile_scale(
                cfg.getint(self._SESSION, "tile_scale", fallback=100))
        except Exception as e:
            logger.debug("Kunne ikke genskabe miniaturestørrelse: %s", e)

    def _save_session(self) -> None:
        cfg = self.config
        try:
            cfg.set(self._SESSION, "geometry",
                    bytes(self.saveGeometry().toBase64()).decode("ascii"))
            cfg.set(self._SESSION, "geometry_version", str(self._GEOMETRY_VERSION))
            if self.page_view is not None:
                cfg.set(self._SESSION, "splitter",
                        ",".join(str(v) for v in self.page_view.splitter.sizes()))
                cfg.set(self._SESSION, "tile_scale",
                        str(self.page_view.tile_scale()))
            cfg.save()
        except Exception as e:
            logger.warning("Kunne ikke gemme sessionen: %s", e)

    def _first_paint(self) -> None:
        if self.page_view is not None:
            self.page_view.rebuild()
            self.page_view.setFocus()

    # ------------------------------------------------------------------
    # IPC
    # ------------------------------------------------------------------
    def _start_ipc(self) -> None:
        from . import ipc
        ipc.start_ipc_server(
            schedule_callback=lambda paths: self._queue.put((self._receive_ipc_paths, (paths,))),
            hwnd=int(self.winId()))

    def _receive_ipc_paths(self, paths: list[str]) -> None:
        self._add_paths(paths)
        self.raise_()
        self.activateWindow()

    # ------------------------------------------------------------------
    # Autoupdater. Logikken (HTTP, hash, procesudloesning) ligger i
    # ``updater.py``; her er kun planlaegning, traadmarshalling og UI.
    # ------------------------------------------------------------------
    def _updates_dir(self) -> Path:
        return utils.get_app_data_path(updater.DOWNLOAD_SUBDIR)

    def start_update_check(self, manual: bool = False) -> None:
        """Ugentligt (eller manuelt udloest) tjek for en nyere version."""
        if self._update_busy:
            if manual:
                self.set_status(_("Søger allerede efter opdateringer…"), transient_ms=4000)
            return
        if not manual:
            if not self.config.getboolean("Updates", "auto_check", fallback=True):
                return
            try:
                last = float(self.config.get("Updates", "last_check", fallback="0") or 0)
            except (TypeError, ValueError):
                last = 0.0
            if time.time() - last < updater.CHECK_INTERVAL_S:
                return

        # Stemplet saettes FOER tjekket, ikke efter. Ellers ville en netvaerksfejl
        # betyde et nyt forsoeg ved hver eneste opstart i stedet for om en uge.
        self.config.set("Updates", "last_check", str(int(time.time())))
        self.config.save()

        self._update_busy = True
        if manual:
            self.set_status(_("Søger efter opdateringer…"), transient_ms=15000)

        current = __version__
        updates_dir = self._updates_dir()

        def worker():
            try:
                updater.purge_old_downloads(updates_dir)
            except Exception as exc:      # oprydning maa aldrig vaelte tjekket
                logger.debug("Oprydning i %s fejlede: %s", updates_dir, exc)
            try:
                info, err = updater.check(current), None
            except Exception as exc:
                info, err = None, exc
            self._queue.put((self._on_update_check_result, (info, manual, err)))

        qt_util.run_in_thread(worker, name="update-check")

    def _on_update_check_result(self, info, manual, err=None) -> None:
        self._update_busy = False
        if err is not None:
            logger.warning("Opdateringstjek mislykkedes: %s", err)
            if manual:
                qt_util.error(self, _("Opdatering"),
                              _("Kunne ikke kontakte serveren:\n%s") % err)
            return
        if info is None or not info.update_available:
            if manual:
                qt_util.info(self, _("Opdatering"),
                             _("Du kører allerede den nyeste version (%s).") % __version__)
            return

        skipped = (self.config.get("Updates", "skipped_version", fallback="") or "").strip()
        if not manual and skipped == info.version:
            logger.info("Version %s er sprunget over af brugeren.", info.version)
            return

        choice = dialogs.UpdateDialog(self, info).run()
        if choice == "skip":
            self.config.set("Updates", "skipped_version", info.version)
            self.config.save()
            self.set_status(_("Version %s springes over.") % info.version, transient_ms=6000)
        elif choice == "install":
            self._run_update_download(info)

    def _run_update_download(self, info) -> None:
        prog = qt_util.ProgressDialog(
            self, _("Henter opdatering"),
            _("Henter Unite Docs %s…") % info.version, cancellable=True)
        prog.show()
        dest = self._updates_dir()
        cancel = prog.cancel_event

        def worker():
            try:
                path = updater.download(
                    info, dest,
                    progress_cb=lambda done, total: self._queue.put(
                        (prog.set_bytes, (done, total))),
                    cancel_event=cancel)
            except Exception as exc:
                self._queue.put((self._update_failed, (prog, exc, info)))
                return
            self._queue.put((self._finish_update, (prog, path, info)))

        qt_util.run_in_thread(worker, name="update-download")

    def _finish_update(self, prog, path, info) -> None:
        prog.finish()
        if not updater.can_install():
            # Koerer fra kildekode: en installer ville skrive et helt andet sted
            # end den koerende app. Peg brugeren mod hjemmesiden i stedet.
            dialogs.UpdateLinkDialog(
                self, _("Opdatering hentet"),
                _("Installationen kan kun køres fra en installeret udgave. "
                  "Filen ligger her, og hentesiden er åbnet i browseren."),
                info, extra_path=path,
                on_open_file=lambda p: self._open_in_explorer(p, select=True)).show()
            return

        qt_util.info(self, _("Installerer opdatering"),
                     _("Unite Docs lukker nu og åbner igen, når version %s er "
                       "installeret.") % info.version)
        try:
            updater.launch_installer(path, Path(sys.executable))
        except Exception as exc:
            logger.error("Kunne ikke starte installeren: %s", exc)
            dialogs.UpdateLinkDialog(
                self, _("Installationen kunne ikke startes"),
                _("Filen blev hentet, men installeren kunne ikke startes:\n%s") % exc,
                info, extra_path=path,
                on_open_file=lambda p: self._open_in_explorer(p, select=True)).show()
            return
        self.close()

    def _update_failed(self, prog, exc, info) -> None:
        prog.finish()
        if isinstance(exc, updater.UpdateCancelled):
            self.set_status(_("Download afbrudt."), transient_ms=5000)
            return
        logger.error("Download af opdatering fejlede: %s", exc)
        dialogs.UpdateLinkDialog(
            self, _("Opdateringen kunne ikke hentes"),
            _("Du kan hente installeren manuelt fra hjemmesiden.\n\n%s") % exc,
            info).show()

    # ------------------------------------------------------------------
    # Livscyklus
    # ------------------------------------------------------------------
    def closeEvent(self, event):  # noqa: N802 - Qt-API
        if self._closing:
            event.accept()
            return
        self._closing = True
        self._save_session()
        self._queue.shutdown()
        try:
            from . import ipc
            ipc.cleanup_ipc()
        except Exception:
            pass
        try:
            utils.purge_stale_temp_files(force_all=True)
            self.page_render_mgr.stop()
            pdf_renderer.clear_doc_cache()
            if self._inserted_dir is not None:
                utils._force_rmtree(self._inserted_dir)
        except Exception as e:
            logger.warning("Oprydning ved lukning fejlede: %s", e)
        event.accept()

    def _on_escape(self) -> None:
        """Escape: afbryd et igangvaerende sidetraek og luk Sorter-panelet."""
        if self.page_view is not None:
            self.page_view.cancel_drag()
        self._close_sort_panel()

    # ------------------------------------------------------------------
    # Kodeords-cache paa disk
    # ------------------------------------------------------------------
    def _get_password_cache_path(self) -> Path:
        return utils.get_app_data_path() / "password_cache.txt"

    def _load_cached_passwords(self) -> list[str]:
        cache_path = self._get_password_cache_path()
        if not cache_path.exists():
            return []
        try:
            with open(cache_path, "r", encoding="utf-8") as f:
                return [line.strip() for line in f if line.strip()]
        except OSError as e:
            logger.error("Fejl ved indlæsning af adgangskode-cache: %s", e)
            return []

    def _save_password_to_cache(self, password: str) -> None:
        """Kaldes ogsaa fra worker-traade. Roerer kun disk, aldrig widgets."""
        if not password:
            return
        cache_path = self._get_password_cache_path()
        try:
            if password not in set(self._load_cached_passwords()):
                with open(cache_path, "a", encoding="utf-8") as f:
                    f.write(password + "\n")
                self._pw_cache = None
        except OSError as e:
            logger.error("Fejl ved lagring af adgangskode til cache: %s", e)

    def _pwlist(self) -> list[str]:
        return list(dict.fromkeys(l.strip() for l in self._pw_lines if l.strip()))

    def _get_all_passwords(self) -> list[str]:
        """GUI-indtastede + cachede kodeord, uden dubletter. Cachet i hukommelsen;
        invalideres af ``_save_password_to_cache``."""
        if self._pw_cache is not None:
            return self._pw_cache
        self._pw_cache = list(dict.fromkeys(self._pwlist() + self._load_cached_passwords()))
        return self._pw_cache

    def clear_password_cache(self) -> None:
        cache_path = self._get_password_cache_path()
        if not cache_path.exists():
            qt_util.info(self, _("Cache"), _("Ingen kodeordscache fundet."))
            return
        try:
            os.remove(cache_path)
            self._pw_cache = None
            qt_util.info(self, _("Cache slettet"), _("Alle gemte kodeord er slettet."))
        except Exception as e:
            qt_util.error(self, _("Fejl"), _("Kunne ikke slette cache: %s") % e)

    # ------------------------------------------------------------------
    # Indsæt side
    # ------------------------------------------------------------------
    def _insert_page_dir(self) -> Path:
        if self._inserted_dir is None:
            self._inserted_dir = utils.new_inserted_pages_dir()
        return self._inserted_dir

    @staticmethod
    def _safe_filename(name: str, fallback: str) -> str:
        """Gør en overskrift brugbar som filnavn. Navnet er ikke kosmetik: det
        bliver kapiteltitlen i md/ePub-eksport (``FileJob.title``)."""
        cleaned = "".join(ch for ch in (name or "") if ch not in '\\/:*?"<>|').strip()
        cleaned = " ".join(cleaned.split())[:60]
        return cleaned or fallback

    def _open_insert_page_dialog(self) -> None:
        if self._insert_dialog is not None:
            self._insert_dialog.show()
            self._insert_dialog.raise_()
            self._insert_dialog.activateWindow()
            return
        dlg = dialogs.InsertPageDialog(self)
        self._insert_dialog = dlg
        dlg.submitted.connect(self._insert_page)
        dlg.destroyed.connect(lambda: setattr(self, "_insert_dialog", None))
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.show()

    def _build_inserted_pdf(self, heading: str, body: str):
        """Skriv den genererede side til session-mappen. Returnerer (sti, antal
        sider) -- en lang fritekst flyder over flere sider -- eller (None, 0)."""
        buf = utils.create_text_page_pdf(heading, body)
        if buf is None:
            qt_util.error(self, _("Indsæt side"), _("Siden kunne ikke oprettes."))
            return None, 0
        target_dir = self._insert_page_dir() / uuid.uuid4().hex
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / (self._safe_filename(heading, _("Ny side")) + ".pdf")
            data = buf.getvalue()
            path.write_bytes(data)
            with pymupdf.open(stream=data, filetype="pdf") as doc:
                count = doc.page_count
        except Exception as e:
            logger.error("Kunne ikke gemme indsat side: %s", e)
            qt_util.error(self, _("Indsæt side"), _("Siden kunne ikke oprettes."))
            return None, 0
        return str(path), count

    def _insert_page(self, heading: str, body: str) -> None:
        """Byg siden og laeg den ind i modellen paa den plads brugeren vaelger."""
        uid = self.page_view.selected_page_uid() if self.page_view else None
        found = self.model.page_by_uid(uid) if uid else None
        if found is None:
            return self._insert_page_as_file(heading, body)
        entry, page = found
        num = self.page_view.page_number_label(uid)
        if num and num.isdigit():
            question = _("Skal siden oprettes før eller efter side %s?") % num
        else:
            question = _("Skal siden oprettes før eller efter den markerede side?")
        where = dialogs.ask_before_after(self, question)
        if where is None:
            return
        path, count = self._build_inserted_pdf(heading, body)
        if not path:
            return
        # Siderne laegges ind i den markerede sides FIL, men med deres egen
        # src_path -- modellen tillader netop det (se edit_model.PageEdit).
        file_index = self.model.index_of_iid(entry.iid)
        pos = entry.pages.index(page) + (1 if where == "after" else 0)
        pages = [em.PageEdit(src_path=path, src_index=i) for i in range(count)]
        self.undo_stack.push(em.insert_pages_cmd(self.model, file_index, pos, pages))
        self.page_view.rebuild()
        self.page_view.select_and_reveal(pages[0].uid)
        self.set_status(_("Side indsat"), transient_ms=4000, kind="success")

    def _insert_page_as_file(self, heading: str, body: str, index=None) -> None:
        """Laeg den genererede side ind som sin EGEN fil (naar ingen side er markeret)."""
        path, count = self._build_inserted_pdf(heading, body)
        if not path:
            return
        entry = em.FileEntry(
            iid=uuid.uuid4().hex, path=path, kind=em.KIND_PDF,
            enc_key=pdf_utils.ENC_NOT_ENCRYPTED, creation_date="",
            source_page_count=count)
        self.model.populate_pages(entry, count)
        at = len(self.model.files) if index is None else index
        self.undo_stack.push(Command(
            "Indsæt side",
            lambda: self.model.add_file(entry, at),
            lambda: self.model.remove_file(entry.iid)))
        if self.page_view is not None:
            self.page_view.rebuild()
        self.set_status(_("Side indsat"), transient_ms=4000, kind="success")

    # ------------------------------------------------------------------
    # Statuslinje
    # ------------------------------------------------------------------
    def _default_status(self) -> str:
        """Tomgangs-teksten: antal filer/sider (+ evt. antal valgte)."""
        files = self.model.files
        n = len(files)
        if n == 0:
            return _("Ingen filer. Træk PDF'er hertil eller klik Tilføj filer.")
        pages = 0
        for fentry in files:
            if fentry.pages_loaded:
                pages += len(fentry.pages)
            elif fentry.kind == em.KIND_IMAGE:
                pages += 1
            else:
                pages += fentry.source_page_count
        text = _("%(n)d filer · %(p)d sider") % {"n": n, "p": pages}
        sel = len(self.page_view.selected_uids()) if self.page_view else 0
        if sel:
            text += _("  ·  %(s)d valgt") % {"s": sel}
        return text

    def set_status(self, text: str = "", *, transient_ms: int | None = None,
                   kind: str = "info") -> None:
        """Skriv en statusbesked (kun UI-traaden -- workere gaar via ``_queue``).

        Tom ``text`` viser tomgangs-teksten. ``transient_ms`` nulstiller til
        tomgang efter et stykke tid. ``kind`` styrer farven."""
        label = getattr(self, "_status_label", None)
        if label is None:
            return
        self._status_timer.stop()
        names = {"info": "Muted", "success": "Success", "warning": "Warning",
                 "error": "Danger"}
        label.setText(text if text else self._default_status())
        label.setObjectName(names.get(kind, "Muted"))
        label.style().unpolish(label)
        label.style().polish(label)
        if transient_ms:
            self._status_timer.start(transient_ms)

    def _refresh_status(self) -> None:
        self.set_status()

    # ------------------------------------------------------------------
    # Model og metadata
    # ------------------------------------------------------------------
    @property
    def paths(self) -> dict:
        """Read-only iid->sti-map udledt af modellen.

        Laeses fra worker-traade, saa der itereres over et atomisk ``list()``-
        snapshot -- en bar comprehension over ``self.model.files`` kunne rejse
        "list changed size during iteration" mens UI-traaden tilfoejer filer."""
        return {f.iid: f.path for f in list(self.model.files)}

    def _add_file_entry(self, path, index="end"):
        """Laeg en fil i modellen som PLADSHOLDER med det samme (ingen disk-I/O
        paa UI-traaden). Metadata laeses asynkront bagefter."""
        if path in self.paths.values():
            return None
        entry = em.FileEntry(
            iid=uuid.uuid4().hex, path=path, kind=em.kind_for_path(path),
            enc_key=pdf_utils.ENC_UNKNOWN, creation_date="", source_page_count=0)
        self.model.add_file(entry, None if index == "end" else index)
        return entry

    def _load_metadata_async(self, entries) -> None:
        """Laes metadata for friskt tilfoejede filer uden for UI-traaden. Én
        worker pr. batch; aabninger serialiseres under ``PDF_LOCK``."""
        entries = [e for e in entries if e is not None]
        if not entries:
            return
        qt_util.run_in_thread(self._metadata_worker,
                              [(e.iid, e.path) for e in entries],
                              self._get_all_passwords(), name="metadata")

    def _metadata_worker(self, snapshot, pw_list) -> None:
        for iid, path in snapshot:
            with pdf_renderer.PDF_LOCK:
                meta = pdf_utils.get_pdf_metadata(path, pw_list)
            self._queue.put((self._apply_file_metadata, (iid, meta)))

    def _apply_file_metadata(self, iid, meta) -> None:
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return
        entry.enc_key = meta["enc_status"]
        entry.creation_date = meta["creation_date"]
        entry.size_bytes = int(meta.get("size_bytes") or 0)     # sorteringsnoegle
        if not entry.pages_loaded:
            entry.source_page_count = int(meta["page_count"]) if meta["page_count"] else 0
        if self.page_view is not None:
            # Hovedet viser navn/dato/haengelaas -- alle tre kan lige have aendret sig.
            self.page_view.refresh_header(iid)
            self._schedule_page_view_refresh()
        # On-add prompt: en netop tilfoejet, stadig-krypteret fil beder om kodeord.
        if iid in self._prompt_on_metadata:
            if entry.enc_key == pdf_utils.ENC_ENCRYPTED:
                self._queue_unlock_prompt(iid)
            else:
                self._prompt_on_metadata.discard(iid)
        if not self._status_timer.isActive():
            self._refresh_status()

    def _schedule_page_view_refresh(self) -> None:
        self._pv_refresh_timer.start(150)

    def _ensure_pages_then_rebuild(self) -> None:
        """Opret sider for enhver laesbar, endnu ikke populeret PDF (i en worker
        under ``PDF_LOCK``) og genopbyg derefter gitteret. Billeder populeres med
        det samme (1 side, ingen I/O)."""
        if self.page_view is None:
            return
        for f in self.model.files:
            if f.kind == em.KIND_IMAGE and not f.pages_loaded:
                self.model.populate_image(f)
        to_load = [f for f in self.model.files
                   if f.kind == em.KIND_PDF and not f.pages_loaded
                   and f.enc_key in (pdf_utils.ENC_NOT_ENCRYPTED, pdf_utils.ENC_DECRYPTED)]
        if not to_load:
            self.page_view.rebuild()
            return
        qt_util.run_in_thread(self._populate_pages_worker,
                              [(f, f.path) for f in to_load],
                              self._get_all_passwords(), name="populate-pages")

    def _populate_pages_worker(self, snapshot, pw_list) -> None:
        counts = []
        for f, path in snapshot:
            n = 0
            with pdf_renderer.PDF_LOCK:
                doc = pdf_utils.open_with_passwords(path, pw_list)
                if doc:
                    try:
                        n = doc.page_count
                    finally:
                        doc.close()
            counts.append((f, n))
        self._queue.put((self._populate_pages_done, (counts,)))

    def _populate_pages_done(self, counts) -> None:
        for f, n in counts:
            # Spring over hvis filen ER populeret: et sent worker-callback maa
            # aldrig overskrive sidelisten, for saa ville en undo af en flytning
            # eller udtraekning blive tromlet ned bagfra.
            if n > 0 and not f.pages_loaded:
                self.model.populate_pages(f, n)
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()
        self._maybe_anonymize_new_files()

    def _refresh_all(self) -> None:
        """Genlaes metadata for hver fil off-thread. Kaldes efter
        kodeordstjek/-gaet, hvor status kan have aendret sig."""
        snapshot = [(f.iid, f.path) for f in self.model.files]
        if snapshot:
            qt_util.run_in_thread(self._metadata_worker, snapshot,
                                  self._get_all_passwords(), name="metadata-refresh")

    # -- hooks som sidevisningen kalder ---------------------------------
    def after_model_change(self) -> None:
        self._refresh_status()

    def after_crop_change(self, uid=None) -> None:
        """En beskaering aendrede sidens maal: genopbyg gitteret, saa flisen
        gen-renderes (crop indgaar i render-cachens noegle)."""
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()

    def _after_history_change(self) -> None:
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()

    def _update_undo_buttons(self) -> None:
        # Qt nedtoner selv ikonet (``QIcon.Disabled`` er lagt ind i
        # ``icons_vector.qicon``), saa her skal kun enabled-flaget saettes.
        self._undo_btn.setEnabled(self.undo_stack.can_undo)
        self._redo_btn.setEnabled(self.undo_stack.can_redo)
        self._history_btn.setEnabled(self.undo_stack.can_undo
                                     or self.undo_stack.can_redo)

    # ------------------------------------------------------------------
    # Fortryd-historik
    # ------------------------------------------------------------------
    def _toggle_history(self) -> None:
        if self._history_panel is not None:
            self._history_panel.close()
            return
        if not (self.undo_stack.can_undo or self.undo_stack.can_redo):
            self.set_status(_("Der er ingen historik endnu."), transient_ms=4000)
            return
        panel = dialogs.HistoryPanel(self, self.undo_stack.undo_labels(),
                                     self.undo_stack.redo_labels())
        panel.seek.connect(self._seek_history)
        panel.destroyed.connect(lambda: setattr(self, "_history_panel", None))
        self._history_panel = panel
        panel.popup_under(self._history_btn)

    def _seek_history(self, applied: int) -> None:
        """Spring til den tilstand hvor praecis ``applied`` kommandoer er kørt."""
        delta = int(applied) - len(self.undo_stack.undo_labels())
        if delta == 0:
            return
        n = (self.undo_stack.redo_many(delta) if delta > 0
             else self.undo_stack.undo_many(-delta))
        if n:
            self._after_history_change()
            self.set_status(
                _("%(n)d trin fortrudt") % {"n": n} if delta < 0
                else _("%(n)d trin gentaget") % {"n": n}, transient_ms=4000)

    def _do_undo(self) -> None:
        if self.undo_stack.can_undo:
            self.undo_stack.undo()
            self._after_history_change()

    def _do_redo(self) -> None:
        if self.undo_stack.can_redo:
            self.undo_stack.redo()
            self._after_history_change()

    def unlock_file(self, iid: str) -> None:
        """Bed om kodeord til én fil (fil-kontekstmenuen i sidevisningen)."""
        if self._prompt_password_for(iid):
            self._ensure_pages_then_rebuild()

    def delete_file(self, iid: str) -> None:
        """Slet en HEL fil (markeret filhoved i sidevisningen)."""
        if self.model.entry_by_iid(iid) is not None:
            self._remove([iid])

    # ------------------------------------------------------------------
    # Tilføj filer, træk-og-slip
    # ------------------------------------------------------------------
    def _add(self) -> None:
        filters = ";;".join([
            qt_util.name_filter(_("Understøttede filer"),
                                "*.pdf *.jpg *.jpeg *.png *.bmp *.tiff *.tif"),
            qt_util.name_filter(_("PDF-filer"), "*.pdf"),
            qt_util.name_filter(_("Billedfiler"),
                                "*.jpg *.jpeg *.png *.bmp *.tiff *.tif"),
        ])
        paths, _sel = QFileDialog.getOpenFileNames(self, _("Tilføj filer"), "", filters)
        if paths:
            self._add_paths(paths)

    def _add_paths(self, paths) -> None:
        added = [self._add_file_entry(p) for p in paths]
        self._mark_for_unlock_prompt(added)
        self._load_metadata_async(added)
        self._after_files_added()

    def _mark_for_unlock_prompt(self, entries) -> None:
        """Husk hvilke friskt tilfoejede filer der skal prompte for kodeord, naar
        deres metadata (og dermed enc-status) lander."""
        for e in entries:
            if e is not None:
                self._prompt_on_metadata.add(e.iid)

    def _after_files_added(self) -> None:
        if self.page_view is not None:
            self._ensure_pages_then_rebuild()
        self._refresh_status()

    # -- native Qt drag & drop (afloeser tkinterdnd2) --------------------
    @staticmethod
    def _dropped_paths(mime) -> list[str]:
        out = []
        for url in mime.urls():
            p = url.toLocalFile()
            if p and os.path.exists(p) and Path(p).suffix.lower() in _SUPPORTED_DROP_EXTENSIONS:
                out.append(p)
        return out

    def dragEnterEvent(self, event):  # noqa: N802 - Qt-API
        if event.mimeData().hasUrls() and self._dropped_paths(event.mimeData()):
            event.acceptProposedAction()
            self.set_status(_("Slip for at tilføje filerne"))
            if self.page_view is not None:
                self.page_view.set_drop_highlight(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 - Qt-API
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):  # noqa: N802 - Qt-API
        if self.page_view is not None:
            self.page_view.set_drop_highlight(False)
        self._refresh_status()
        event.accept()

    def dropEvent(self, event):  # noqa: N802 - Qt-API
        if self.page_view is not None:
            self.page_view.set_drop_highlight(False)
        paths = self._dropped_paths(event.mimeData())
        if not paths:
            event.ignore()
            return
        # Indsaet paa drop-positionen over sidegitteret, ikke altid til sidst.
        insert_index = "end"
        if self.page_view is not None:
            idx = self.page_view.drop_file_index(event.globalPosition().toPoint())
            if idx is not None:
                insert_index = idx
        added = []
        for p in paths:
            entry = self._add_file_entry(p, index=insert_index)
            if entry is not None:
                added.append(entry)
                if insert_index != "end":
                    insert_index += 1     # hold slupne filer i raekkefoelge
        self._mark_for_unlock_prompt(added)
        self._load_metadata_async(added)
        self._after_files_added()
        event.acceptProposedAction()

    # ------------------------------------------------------------------
    # Rækkefølge og redigering
    # ------------------------------------------------------------------
    def _remove(self, iids_to_remove: list[str] | None = None) -> None:
        """Fjern hele filer fra modellen (kildefilerne paa disken roeres ikke)."""
        if iids_to_remove is None:
            sel = self.page_view.selected_file() if self.page_view else None
            iids_to_remove = [sel] if sel else []
        iids = [i for i in iids_to_remove if self.model.entry_by_iid(i) is not None]
        if not iids:
            return
        removed = [(self.model.index_of_iid(i), self.model.entry_by_iid(i)) for i in iids]

        def do():
            for iid in iids:
                self.model.remove_file(iid)

        def undo():
            for idx, entry in sorted(removed, key=lambda t: t[0]):
                self.model.add_file(entry, idx)

        self.undo_stack.push(Command("Slet fil", do, undo))
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()

    def _delete_selection(self) -> None:
        """Kommandobarens Slet: sider hvis sider er markeret, ellers hele filen."""
        if self.page_view is None:
            return
        sel = self.page_view.selected_uids()
        if sel:
            self.page_view.delete_pages(sel)
        elif self.page_view.selected_file():
            self._remove([self.page_view.selected_file()])

    def _rotate_left(self) -> None:
        if self.page_view is not None:
            self.page_view.rotate_selected(-90)

    def _rotate_right(self) -> None:
        if self.page_view is not None:
            self.page_view.rotate_selected(90)

    def _nudge(self, direction: int) -> None:
        """Pil op/ned. Sider flyttes én plads; ved filens kant vandrer de over i
        nabofilen, og findes der ingen, bliver de deres egen fil. Er en HEL fil
        markeret, flyttes filen (med sine klaebende boern) i stedet."""
        if self.page_view is None:
            return
        sel = self.page_view.selected_uids()
        if sel:
            cmd = em.nudge_pages_cmd(self.model, sel, direction)
            if cmd is None:
                return
            self.undo_stack.push(cmd)
        elif self.page_view.selected_file():
            self.undo_stack.push(em.move_files_cmd(
                self.model, [self.page_view.selected_file()], direction))
        else:
            return
        self.page_view.rebuild()
        self.page_view.reselect(sel)

    def _up(self) -> None:
        self._nudge(-1)

    def _down(self) -> None:
        self._nudge(1)

    def _move_edge(self, to_end: bool) -> None:
        """Flyt markeringen helt til dokumentets start/slut.

        Sider rives UD som deres egen fil forrest/bagerst -- ikke ind i den
        foerste/sidste eksisterende fil. "Flyt oeverst" paa en side midt i et
        dokument skal give en selvstaendig side foran alt andet; smed man den i
        stedet ind i nabofilen, blandede den sig med et helt andet dokument.
        En markeret FIL flytter derimod hele sin blok (med klaebende boern)."""
        if self.page_view is None or not self.model.files:
            return
        sel = self.page_view.selected_uids()
        if sel:
            entry = self.model.page_by_uid(sel[0])[0]
            # Er filen allerede yderst og bestaar kun af markeringen, er der intet
            # at goere -- ellers ville vi slette og genskabe en identisk fil.
            edge = self.model.files[-1 if to_end else 0]
            if entry is edge and len(entry.pages) == len(sel):
                return
            self.undo_stack.push(em.extract_pages_cmd(
                self.model, sel, at_index=len(self.model.files) if to_end else 0))
        elif self.page_view.selected_file():
            iid = self.page_view.selected_file()
            block = [iid] + [f.iid for f in self.model.files if f.origin_iid == iid]
            rest = [f.iid for f in self.model.files if f.iid not in set(block)]
            self.undo_stack.push(em.reorder_files_cmd(
                self.model, rest + block if to_end else block + rest))
        else:
            return
        self.page_view.rebuild()
        self.page_view.reselect(sel)

    def _move_top(self) -> None:
        self._move_edge(False)

    def _move_bottom(self) -> None:
        self._move_edge(True)

    def _toggle_crop_tool(self) -> None:
        """Slaa beskaerings-vaerktoejet til i fremviseren."""
        if self.page_view is not None:
            self.page_view.activate_crop_tool()

    def set_crop_active(self, active: bool) -> None:
        """Sidevisningen melder tilbage naar beskaering slaas fra/til, saa
        knappen i kommandobaren viser den faktiske tilstand."""
        self._crop_btn.setChecked(bool(active))

    # ------------------------------------------------------------------
    # Sortering
    # ------------------------------------------------------------------
    def _toggle_sort_panel(self) -> None:
        if self._sort_panel is not None:
            self._close_sort_panel()
            return
        panel = dialogs.SortPanel(self, self._SORT_ROWS, self._sort_dir)
        panel.chosen.connect(self._apply_sort)
        panel.destroyed.connect(self._on_sort_panel_closed)
        self._sort_panel = panel
        self._sort_btn.setChecked(True)
        panel.popup_under(self._sort_btn)

    def _on_sort_panel_closed(self) -> None:
        self._sort_panel = None
        self._sort_btn.setChecked(False)

    def _close_sort_panel(self) -> None:
        panel, self._sort_panel = self._sort_panel, None
        self._sort_btn.setChecked(False)
        if panel is not None:
            panel.close()

    def _apply_sort(self, key: str) -> None:
        if not self.model.files:
            return
        reverse = self._sort_dir.get(key, False)
        self.undo_stack.push(em.sort_files_cmd(self.model, key, reverse))
        if key != em.SORT_REVERSE:
            # Andet klik paa samme noegle vender retningen.
            self._sort_dir[key] = not reverse
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()
        # Panelet bliver staaende (det er hele pointen) -- gentegn kun pilene.
        if self._sort_panel is not None:
            btn = self._sort_btn
            self._close_sort_panel()
            panel = dialogs.SortPanel(self, self._SORT_ROWS, self._sort_dir)
            panel.chosen.connect(self._apply_sort)
            panel.destroyed.connect(self._on_sort_panel_closed)
            self._sort_panel = panel
            self._sort_btn.setChecked(True)
            panel.popup_under(btn)

    # ------------------------------------------------------------------
    # Maskering: bekræftelse før gem
    # ------------------------------------------------------------------
    @staticmethod
    def _count_redactions(entries) -> int:
        return sum(1 for f in entries for pg in getattr(f, "pages", [])
                   for a in pg.annots if a.kind == annotations.ANNOT_REDACT)

    def _confirm_redactions(self, entries) -> bool:
        """Maskering er uigenkaldelig i outputtet (``apply_redactions`` skriver
        indholdsstroemmen om). Advar foer ethvert gem der bager den ind."""
        n = self._count_redactions(entries)
        if n == 0:
            return True
        return qt_util.ask_yes_no(
            self, _("Bekræft maskering"),
            _("Dokumentet indeholder %s maskering(er), som fjerner indhold "
              "permanent i den gemte fil og ikke kan fortrydes i outputtet. "
              "Vil du fortsætte?") % n, dangerous=True)

    # ------------------------------------------------------------------
    # Per-side eksport
    # ------------------------------------------------------------------
    def export_single_page(self, page_uid, fmt) -> None:
        """Eksportér ÉN side til sin egen fil. ``fmt`` i {pdf, md, epub, jpg, png}.
        JPG/PNG holdes bevidst UDE af ``export_formats.FORMAT_ORDER`` (de er
        meningsloese i flet-dialogen); de rasterer den faerdige side."""
        found = self.model.page_by_uid(page_uid)
        if not found:
            return
        entry, page = found
        # En indsat side har sin egen (ukrypterede) kildefil -- laase-tjekket
        # gaelder kun sider der faktisk kommer fra entry.path.
        own = os.path.normcase(page.src_path) == os.path.normcase(entry.path)
        if (own and entry.kind == em.KIND_PDF
                and entry.enc_key not in (pdf_utils.ENC_DECRYPTED,
                                          pdf_utils.ENC_NOT_ENCRYPTED)):
            qt_util.info(self, _("Eksport"), _("Siden kan ikke eksporteres (låst fil)."))
            return
        ext = {"pdf": ".pdf", "md": ".md", "epub": ".epub",
               "jpg": ".jpg", "png": ".png"}.get(fmt)
        if not ext:
            return
        if any(a.kind == annotations.ANNOT_REDACT for a in page.annots):
            if not self._confirm_redactions([entry]):
                return
        stem = Path(page.src_path).stem
        suggested = "%s_side%d%s" % (stem, page.src_index + 1, ext)
        out_path, _sel = QFileDialog.getSaveFileName(
            self, _("Eksportér side"), suggested,
            qt_util.name_filter(fmt.upper(), "*" + ext))
        if not out_path:
            return
        pdf_renderer.clear_doc_cache()
        job = save_pipeline.FileJob(
            path=page.src_path,
            kind=entry.kind if own else em.kind_for_path(page.src_path),
            enc_key=entry.enc_key if own else pdf_utils.ENC_NOT_ENCRYPTED,
            pages=[save_pipeline.PageJob(src_index=page.src_index,
                                         rotation=page.rotation, annots=page.annots)],
            title=stem)
        prog = qt_util.ProgressDialog(
            self, _("Eksporterer…"), _("Eksporterer side til %s…") % fmt.upper(),
            cancellable=True)
        prog.show()
        self._export_progress = prog
        qt_util.run_in_thread(self._export_page_worker, out_path, job,
                              self._get_all_passwords(), fmt, stem, prog,
                              name="export-page")

    def _export_page_worker(self, out_path, job, pw, fmt, stem, prog) -> None:
        ev = prog.cancel_event

        def cancel():
            return ev is not None and ev.is_set()

        def export_progress(done, total):
            self._queue.put((prog.set_page_progress, (done, total)))

        try:
            if fmt in ("pdf", "jpg", "png"):
                merged, ok, fail, chapters = save_pipeline.build_document(
                    [job], pw, is_pdf=True,
                    apply_annots=annotations.apply_specs_to_page)
                try:
                    if ok == 0:
                        raise RuntimeError(_("Siden kunne ikke behandles."))
                    if fmt == "pdf":
                        merged.save(out_path, garbage=3, deflate=True)
                    else:
                        merged[0].get_pixmap(dpi=200).save(out_path)
                finally:
                    merged.close()
            else:
                fmt_const = (export_formats.FORMAT_MD if fmt == "md"
                             else export_formats.FORMAT_EPUB)
                merged, ok, fail, chapters = save_pipeline.build_document(
                    [job], pw, is_pdf=False,
                    apply_annots=annotations.apply_specs_to_page)
                try:
                    if ok == 0:
                        raise RuntimeError(_("Siden kunne ikke behandles."))
                    export_formats.write_document(
                        fmt_const, doc=merged, out_path=out_path, chapters=chapters,
                        title=stem, progress=export_progress, cancel=cancel)
                finally:
                    merged.close()
            self._queue.put((self._export_page_done, (out_path,)))
        except export_formats.ExportCancelled:
            self._queue.put((self._export_page_cancelled, (out_path,)))
        except Exception as e:
            logger.error("Per-side eksport fejlede: %s", e)
            self._queue.put((self._export_page_failed, (str(e),)))

    # ------------------------------------------------------------------
    # Eksport af en markering
    # ------------------------------------------------------------------
    def export_pages(self, page_uids, fmt) -> None:
        """Eksportér de markerede sider. ``fmt`` i {pdf, md, epub, jpg, png}.

        PDF/Markdown/ePub bliver ÉT dokument med siderne i den raekkefoelge de
        staar. JPG og PNG kan ikke rumme flere sider, saa dér vaelges en mappe
        og der skrives én fil pr. side.
        """
        uids = [u for u in page_uids if self.model.page_by_uid(u)]
        if not uids:
            return
        if len(uids) == 1:
            return self.export_single_page(uids[0], fmt)
        if fmt not in ("pdf", "md", "epub", "jpg", "png"):
            return

        only = set(uids)
        entries = self._scoped_entries(only)
        laaste = [e for e in entries
                  if e.kind == em.KIND_PDF
                  and e.enc_key not in (pdf_utils.ENC_DECRYPTED,
                                        pdf_utils.ENC_NOT_ENCRYPTED)]
        if laaste:
            qt_util.info(self, _("Eksport"),
                         _("Nogle af siderne ligger i låste filer og kan ikke "
                           "eksporteres:\n\n%s")
                         % "\n".join(Path(e.path).name for e in laaste))
            return
        if not self._confirm_redactions(entries):
            return

        pdf_renderer.clear_doc_cache()
        stem = _("Valgte sider")
        if fmt in ("jpg", "png"):
            dest = QFileDialog.getExistingDirectory(
                self, _("Vælg mappe til de eksporterede sider"))
            if not dest:
                return
            out_target = dest
        else:
            ext = "." + fmt
            out_target, _sel = QFileDialog.getSaveFileName(
                self, _("Eksportér sider"), stem + ext,
                qt_util.name_filter(fmt.upper(), "*" + ext))
            if not out_target:
                return
            if self._is_path_locked(out_target):
                qt_util.error(self, _("Filen er i brug"),
                              _("Filen \"%s\" er åben i et andet program.\n"
                                "Luk den og prøv igen.") % out_target)
                return

        jobs = save_pipeline.build_jobs(entries, only_uids=only)
        prog = qt_util.ProgressDialog(
            self, _("Eksporterer…"),
            _("Eksporterer %(n)d sider til %(fmt)s…")
            % {"n": len(uids), "fmt": fmt.upper()}, cancellable=True)
        prog.show()
        self._export_progress = prog
        qt_util.run_in_thread(self._export_pages_worker, out_target, jobs,
                              self._get_all_passwords(), fmt, stem, prog,
                              name="export-pages")

    def _export_pages_worker(self, out_target, jobs, pw, fmt, stem, prog) -> None:
        cb = self._progress_reporters(prog)
        try:
            if fmt in ("jpg", "png"):
                merged, ok, _fail, _chapters = save_pipeline.build_document(
                    jobs, pw, is_pdf=True,
                    apply_annots=annotations.apply_specs_to_page,
                    report=cb["progress_report"], cancel=cb["cancel"])
                try:
                    if ok == 0:
                        raise RuntimeError(_("Siderne kunne ikke behandles."))
                    for i in range(merged.page_count):
                        if cb["cancel"]():
                            raise export_formats.ExportCancelled()
                        navn = "%s_%03d.%s" % (stem, i + 1, fmt)
                        merged[i].get_pixmap(dpi=200).save(
                            str(Path(out_target) / navn))
                        self._queue.put((prog.set_page_progress,
                                         (i + 1, merged.page_count)))
                finally:
                    merged.close()
                self._queue.put((self._export_pages_done, (out_target, True)))
                return

            fmt_const = {"pdf": export_formats.FORMAT_PDF,
                         "md": export_formats.FORMAT_MD,
                         "epub": export_formats.FORMAT_EPUB}[fmt]
            save_pipeline.merge_worker(
                out_target, jobs, pw, fmt_const,
                apply_annots=annotations.apply_specs_to_page,
                report=cb["progress_report"], ocr_report=cb["ocr_report"],
                export_report=cb["export_report"],
                export_progress=cb["export_progress"], cancel=cb["cancel"])
            self._queue.put((self._export_pages_done, (out_target, False)))
        except export_formats.ExportCancelled:
            self._queue.put((self._export_page_cancelled, (out_target,)))
        except Exception as e:
            logger.error("Eksport af markering fejlede: %s", e)
            self._queue.put((self._export_page_failed, (str(e),)))

    def _export_pages_done(self, out_target, is_folder) -> None:
        self._close_export_progress()
        self._show_saved_dialog(
            _("Eksport"),
            _("Siderne er eksporteret til mappen:") if is_folder
            else _("Siderne er eksporteret som:"),
            Path(out_target), is_folder=is_folder)

    def _close_export_progress(self) -> None:
        if self._export_progress is not None:
            self._export_progress.finish()
            self._export_progress = None

    def _export_page_done(self, out_path) -> None:
        self._close_export_progress()
        self._show_saved_dialog(_("Eksport"), _("Siden er eksporteret som:"),
                                Path(out_path), is_folder=False)

    def _export_page_failed(self, msg) -> None:
        self._close_export_progress()
        qt_util.error(self, _("Eksport fejlede"), msg)

    def _export_page_cancelled(self, out_path) -> None:
        self._close_export_progress()
        self._discard(out_path)
        qt_util.info(self, _("Annulleret"), _("Eksporten blev annulleret."))

    @staticmethod
    def _discard(path) -> None:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Gem enkeltfiler / flet
    # ------------------------------------------------------------------
    @staticmethod
    def _is_path_locked(path) -> bool:
        """True hvis filen findes men ikke kan aabnes til skrivning -- typisk
        fordi den er aaben i et andet program. En fil der ikke findes endnu er
        aldrig laast."""
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r+b"):
                pass
            return False
        except OSError:
            return True

    def _save_dec(self) -> None:
        # Bed om kodeord til laaste filer foerst, saa de kan komme med.
        self._ensure_all_unlocked()
        only_uids = self._page_scope()
        pdf_iids = [f.iid for f in self._scoped_entries(only_uids)
                    if f.enc_key in (pdf_utils.ENC_DECRYPTED, pdf_utils.ENC_NOT_ENCRYPTED)]
        if not pdf_iids:
            qt_util.info(self, _("Gem"), _("Ingen PDF-filer at gemme."))
            return

        # Slip kildehaandtag sidevisningen holder, saa et gem ikke selv-laaser dem.
        pdf_renderer.clear_doc_cache()
        fmt = dialogs.ask_export_format(self)
        if not fmt:
            return
        dest_folder = QFileDialog.getExistingDirectory(
            self, _("Vælg mappe til at gemme filer"))
        if not dest_folder:
            return

        entries = [e for e in (self.model.entry_by_iid(i) for i in pdf_iids) if e is not None]
        if not self._confirm_redactions(entries):
            return
        jobs = save_pipeline.build_jobs(entries, only_uids=only_uids)
        if not jobs:
            qt_util.warn(self, _("Ingen sider"), _("Der er ingen sider at gemme."))
            return

        # Tjek at ingen maalfil er laast af et andet program FOER arbejdet starter.
        # En fil der blot findes haandteres af ``_unique_save_path`` med et
        # _1-suffiks -- kun en aaben fil skal stoppe os.
        extension = export_formats.extension_for(fmt)
        is_pdf = fmt == export_formats.FORMAT_PDF
        locked = []
        for job in jobs:
            op = Path(job.path)
            if is_pdf:
                name = (op.stem + "_dekrypteret.pdf"
                        if job.enc_key == pdf_utils.ENC_DECRYPTED else op.name)
            else:
                name = op.stem + extension
            if self._is_path_locked(str(Path(dest_folder) / name)):
                locked.append(name)
        if locked:
            qt_util.error(self, _("Filen er i brug"),
                          _("Følgende fil(er) er åben i et andet program:\n\n%s\n\n"
                            "Luk dem og prøv igen.") % "\n".join(locked))
            return

        prog = qt_util.ProgressDialog(
            self, _("Gemmer filer"),
            _("Forbereder at gemme %s filer...") % len(jobs), cancellable=True)
        prog.show()
        self.set_status(_("Gemmer…"))
        qt_util.run_in_thread(self._save_dec_worker, jobs, self._get_all_passwords(),
                              prog, dest_folder, fmt, name="save-each")

    def _progress_reporters(self, prog):
        """De fem callbacks ``save_pipeline`` forventer, alle marshallet til
        UI-traaden. Samlet ét sted, fordi gem og flet bruger dem ens."""
        ev = prog.cancel_event
        return {
            "cancel": lambda: ev is not None and ev.is_set(),
            "progress_report": lambda i, total: self._queue.put(
                (self._save_progress, (prog, i, total))),
            "ocr_report": lambda: self._queue.put(
                (prog.set_status, (_("Gør scannede sider søgbare (OCR)..."),))),
            "export_report": lambda ext: self._queue.put(
                (prog.set_status, (_("Konverterer til %s...") % ext,))),
            "export_progress": lambda done, total: self._queue.put(
                (prog.set_page_progress, (done, total))),
        }

    @staticmethod
    def _save_progress(prog, current, total) -> None:
        prog.set_fraction(current, total)
        prog.set_status(_("Behandler fil %s af %s...") % (current, total))

    def _save_dec_worker(self, jobs, pw_list, prog, dest_folder, fmt) -> None:
        cb = self._progress_reporters(prog)
        try:
            res = save_pipeline.save_each_worker(
                jobs, pw_list, fmt, dest_folder,
                apply_annots=annotations.apply_specs_to_page,
                progress_report=cb["progress_report"], ocr_report=cb["ocr_report"],
                export_report=cb["export_report"],
                export_progress=cb["export_progress"], cancel=cb["cancel"])
        except export_formats.ExportCancelled:
            self._queue.put((self._save_dec_cancelled, (prog,)))
            return
        except Exception as e:
            self._queue.put((self._merge_error, (e, prog)))
            return
        self._queue.put((self._save_dec_complete,
                         (res["saved"], res["failed"], res["missing_pages"],
                          prog, dest_folder)))

    def _save_dec_cancelled(self, prog) -> None:
        prog.finish()
        qt_util.info(self, _("Annulleret"), _("Eksporten blev annulleret."))

    def _save_dec_complete(self, saved_count, failed_count, missing_pages,
                           prog, dest_folder=None) -> None:
        prog.finish()
        if failed_count > 0:
            self.set_status(_("%(ok)d gemt · %(fail)d fejlede")
                            % {"ok": saved_count, "fail": failed_count},
                            transient_ms=8000, kind="warning")
            qt_util.warn(self, _("Gem"),
                         _("%(ok)s filer gemt, %(fail)s fejlede.\nSe loggen for detaljer.")
                         % {"ok": saved_count, "fail": failed_count})
        elif saved_count > 0:
            self.set_status(_("%(n)d filer gemt") % {"n": saved_count},
                            transient_ms=8000, kind="success")
            msg = _("%s filer gemt.") % saved_count
            if missing_pages > 0:
                msg += "\n" + _("%s side(r) uden tekstlag blev ikke udtrukket") % missing_pages
            if dest_folder:
                msg += "\n\n" + _("Filerne ligger i:")
                self._show_saved_dialog(_("Gem"), msg, Path(dest_folder), is_folder=True)
            else:
                qt_util.info(self, _("Gem"), msg)

    def _page_scope(self) -> set | None:
        """Sidegitterets markering, naar den skal styre en filhandling.

        **Mindst to sider.** Én markeret side er ikke et valg -- der er altid
        praecis én side markeret, ogsaa naar man bare har klikket sig frem -- og
        et "Flet og gem" der pludselig gemte den ene side ville vaere en faelde.
        ``None`` betyder "hele dokumentet".
        """
        if self.page_view is None:
            return None
        sel = self.page_view.grid.selected_uids()
        return set(sel) if len(sel) > 1 else None

    def _scoped_entries(self, only_uids) -> list:
        """Filerne der bidrager med mindst én side inden for ``only_uids``."""
        if only_uids is None:
            return list(self.model.files)
        return [f for f in self.model.files
                if any(p.uid in only_uids for p in f.pages)]

    def _merge(self) -> None:
        if not self.model.files:
            qt_util.warn(self, _("Ingen filer"), _("Tilføj filer først"))
            return
        self._ensure_all_unlocked()
        only_uids = self._page_scope()
        entries = self._scoped_entries(only_uids)
        if not self._confirm_redactions(entries):
            return
        pdf_renderer.clear_doc_cache()
        fmt = dialogs.ask_export_format(self)
        if not fmt:
            return
        spec = export_formats.EXPORT_FORMATS[fmt]
        suggested = (_("Valgte sider") if only_uids else _("Flettet")) + spec.extension
        out_path, _sel = QFileDialog.getSaveFileName(
            self, _("Flet og gem"), suggested,
            ";;".join(qt_util.name_filter(lbl, pats)
                      for lbl, pats in export_formats.format_filetypes(fmt)))
        if not out_path:
            return
        # Tjek at maalfilen ikke er laast FOER fletningen starter -- ellers
        # opdages det foerst ved save, efter alt arbejdet er gjort.
        if self._is_path_locked(out_path):
            qt_util.error(self, _("Filen er i brug"),
                          _("Filen \"%s\" er åben i et andet program.\n"
                            "Luk den og prøv igen.") % out_path)
            return
        jobs = save_pipeline.build_jobs(entries, only_uids=only_uids)
        if not jobs:
            qt_util.warn(self, _("Ingen sider"), _("Der er ingen sider at flette."))
            return
        prog = qt_util.ProgressDialog(
            self, _("Fletter PDF'er"),
            _("Forbereder at flette %s filer...") % len(jobs), cancellable=True)
        prog.show()
        self.set_status(_("Fletter…"))
        qt_util.run_in_thread(self._merge_worker, out_path,
                              self._get_all_passwords(), prog, fmt, jobs,
                              name="merge")

    def _merge_worker(self, out_path, pw_list, prog, fmt, jobs) -> None:
        cb = self._progress_reporters(prog)
        try:
            res = save_pipeline.merge_worker(
                out_path, jobs, pw_list, fmt,
                apply_annots=annotations.apply_specs_to_page,
                report=cb["progress_report"], ocr_report=cb["ocr_report"],
                export_report=cb["export_report"],
                export_progress=cb["export_progress"], cancel=cb["cancel"])
            self._queue.put((self._merge_complete,
                             (res["ok"], res["fail"], res["missing_pages"],
                              out_path, prog)))
        except export_formats.ExportCancelled:
            self._queue.put((self._merge_cancelled, (out_path, prog)))
        except Exception as e:
            self._queue.put((self._merge_error, (e, prog)))

    def _merge_complete(self, ok, fail, missing_pages, out_path, prog) -> None:
        prog.finish()
        self.set_status(_("Gemt: %(name)s") % {"name": Path(out_path).name},
                        transient_ms=8000, kind="success")
        msg = _("%(ok)s filer behandlet korrekt\n%(fail)s filer fejlede") \
            % {"ok": ok, "fail": fail}
        if missing_pages > 0:
            msg += "\n" + _("%s side(r) uden tekstlag blev ikke udtrukket") % missing_pages
        msg += "\n\n" + _("Filen er gemt som:")
        self._show_saved_dialog(_("Fletning fuldført"), msg, Path(out_path),
                                is_folder=False)

    def _merge_cancelled(self, out_path, prog) -> None:
        prog.finish()
        self._discard(out_path)
        qt_util.info(self, _("Annulleret"), _("Eksporten blev annulleret."))

    def _merge_error(self, error, prog) -> None:
        prog.finish()
        self.set_status(_("Fletning fejlede"), transient_ms=8000, kind="error")
        qt_util.error(self, _("Fejl"), _("Kunne ikke gemme filen:\n%s") % error)

    # ------------------------------------------------------------------
    # Kvitteringer og Stifinder
    # ------------------------------------------------------------------
    def _open_in_explorer(self, target: Path, select: bool = False) -> None:
        """Aabn en gemt fil (eller dens mappe) i Stifinder. ``select=True``
        aabner mappen med filen markeret."""
        try:
            target = Path(target)
            if select and target.exists():
                subprocess.Popen(["explorer", "/select,", str(target)])
            else:
                os.startfile(str(target))       # noqa: S606 - Windows-app
        except Exception as e:
            logger.warning("Kunne ikke aabne %s: %s", target, e)
            self.set_status(_("Kunne ikke åbne %s") % target, transient_ms=6000,
                            kind="warning")

    def _show_saved_dialog(self, title, message, target: Path, *, is_folder: bool):
        dlg = dialogs.SavedDialog(
            self, title, message, target, is_folder=is_folder,
            on_open=lambda p, sel: self._open_in_explorer(p, select=sel))
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.show()
        return dlg

    def _show_credits(self) -> None:
        dialogs.CreditsDialog(self).exec()

    # ------------------------------------------------------------------
    # Kodeord: dialog, prompt, tjek og gætning
    # ------------------------------------------------------------------
    def _open_passwords_dialog(self) -> None:
        if self._pw_dialog is not None:
            self._pw_dialog.show()
            self._pw_dialog.raise_()
            self._pw_dialog.activateWindow()
            return
        dlg = dialogs.PasswordsDialog(self, self._pw_lines)
        self._pw_dialog = dlg
        dlg.committed.connect(self._commit_pw_lines)
        dlg.check_requested.connect(self._start_check_only)
        dlg.guess_requested.connect(self._start_guessing)
        dlg.destroyed.connect(lambda: setattr(self, "_pw_dialog", None))
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        dlg.show()

    def _commit_pw_lines(self, lines: list[str]) -> None:
        self._pw_lines = list(lines)
        self._pw_cache = None

    def _append_password(self, p: str) -> None:
        """Tilfoej et kodeord til app-tilstanden (UI-traaden). Opdaterer den
        aabne kodeord-dialog hvis den er fremme."""
        p = (p or "").strip()
        if not p or p in self._pw_lines:
            return
        self._pw_lines.append(p)
        self._pw_cache = None
        if self._pw_dialog is not None:
            self._pw_dialog.sync_lines(self._pw_lines)

    def _mark_unlocked(self, iid: str, pw: str | None) -> None:
        """Markér en fil som dekrypteret, gem kodeordet og genopfrisk visningen."""
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return
        entry.enc_key = pdf_utils.ENC_DECRYPTED
        if pw:
            self._append_password(pw)
            self._save_password_to_cache(pw)
        self._prompt_on_metadata.discard(iid)
        # Flisen viste haengelaasen; gen-render nu den er laesbar.
        self.page_render_mgr.invalidate_path(entry.path)
        if self.page_view is not None:
            self.page_view.refresh_header(iid)
            self._schedule_page_view_refresh()

    def _try_known_unlock(self, iid: str) -> bool:
        """Proev de kendte kodeord (GUI + cache) mod filen. True hvis den aabner
        (eller ikke er krypteret)."""
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return False
        if entry.enc_key != pdf_utils.ENC_ENCRYPTED:
            return True
        doc = pdf_utils.open_with_passwords(entry.path, self._get_all_passwords())
        if doc is not None:
            doc.close()
            self._mark_unlocked(iid, None)   # kodeordet er allerede kendt
            return True
        return False

    def _prompt_password_for(self, iid: str) -> bool:
        """Modal: bed om kodeord til én krypteret fil. Proever de kendte kodeord
        foerst. Tilbyder at gaette. True hvis filen blev laast op."""
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return False
        if entry.enc_key != pdf_utils.ENC_ENCRYPTED:
            return True
        if self._try_known_unlock(iid):
            return True

        def verify(pw: str) -> bool:
            doc = pdf_utils.open_with_passwords(entry.path, [pw])
            if doc is None:
                return False
            doc.close()
            return True

        dlg = dialogs.PasswordPromptDialog(self, Path(entry.path).name, verify)
        dlg.exec()
        if dlg.password:
            self._mark_unlocked(iid, dlg.password)
            return True
        if dlg.guess_requested:
            # Brugeren kender ikke kodeordet -> gaet. Afbryd resten af koeen, saa
            # der ikke stables prompts oven paa gaette-dialogen.
            self._pending_unlock.clear()
            self._start_guessing(items=[iid])
        return False

    def _queue_unlock_prompt(self, iid: str) -> None:
        """Saet en fil i koe til en modal kodeord-prompt (bruges ved tilfoejelse)."""
        if iid not in self._pending_unlock:
            self._pending_unlock.append(iid)
        QTimer.singleShot(0, self._drain_unlock_prompts)

    def _drain_unlock_prompts(self) -> None:
        """Koer de koeede prompts én ad gangen (aldrig genindtraedende)."""
        if self._unlocking:
            return
        self._unlocking = True
        try:
            while self._pending_unlock:
                iid = self._pending_unlock.pop(0)
                entry = self.model.entry_by_iid(iid)
                if entry is None or entry.enc_key != pdf_utils.ENC_ENCRYPTED:
                    continue
                self._prompt_password_for(iid)
        finally:
            self._unlocking = False

    def _ensure_all_unlocked(self) -> None:
        """Prompt for hver stadig-laast krypteret fil foer flet/gem. Fortsaetter
        uanset (pipelinen rapporterer selv filer der stadig er laaste)."""
        for iid in [f.iid for f in self.model.files]:
            entry = self.model.entry_by_iid(iid)
            if entry is not None and entry.enc_key == pdf_utils.ENC_ENCRYPTED:
                if not self._try_known_unlock(iid):
                    self._prompt_password_for(iid)

    def _encrypted_iids(self) -> list[str]:
        return [f.iid for f in self.model.files if f.enc_key == pdf_utils.ENC_ENCRYPTED]

    def _open_guess_dialog(self, title: str, items):
        dlg = dialogs.GuessProgressDialog(
            self, title, items, lambda iid: Path(self.paths[iid]).name)
        dlg.closed.connect(self._refresh_all)
        dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self._guess_dialog = dlg
        dlg.show()
        return dlg

    def _start_check_only(self, items=None) -> None:
        items = items or self._encrypted_iids()
        if not items:
            qt_util.info(self, _("Tjek kodeliste"),
                         _("Der er ingen krypterede PDF-filer på listen."))
            return
        dlg = self._open_guess_dialog(_("Tjekker kodeord fra liste"), items)
        # Snapshot kodeordene paa UI-traaden; workeren maa ikke laese widgets.
        qt_util.run_in_thread(self._check_passwords_only_worker, items,
                              self._get_all_passwords(), dlg, name="pw-check")

    def _check_passwords_only_worker(self, items, known_passwords, dlg) -> None:
        known = list(known_passwords)
        stop = dlg.stop_event
        for iid in items:
            if stop.is_set():
                break
            self._queue.put((dlg.start_row, (iid,)))
            found_pw = None
            for pw in known:
                if stop.is_set():
                    break
                if pdf_utils.open_with_passwords(self.paths[iid], [pw]):
                    found_pw = pw
                    break
            self._report_guess_result(dlg, iid, found_pw, known)
            if not stop.is_set():
                time.sleep(0.05)
        self._queue.put((dlg.mark_finished, ()))
        self._queue.put((self._refresh_all, ()))

    def _report_guess_result(self, dlg, iid, found_pw, known) -> None:
        """Faelles efterbehandling for tjek og gaet (kaldes fra worker-traaden)."""
        if found_pw:
            if found_pw not in known:
                known.append(found_pw)
            # Dedup sker paa UI-traaden inde i ``_append_password``.
            self._queue.put((self._append_password, (found_pw,)))
            self._save_password_to_cache(found_pw)
            self._queue.put((dlg.set_row_status, (iid, _("Fundet: %s") % found_pw)))
            self.page_render_mgr.invalidate_path(self.paths[iid])
        else:
            self._queue.put((dlg.set_row_status, (iid, _("Ikke fundet"))))
        self._queue.put((dlg.stop_row, (iid,)))

    def _start_guessing(self, items=None) -> None:
        opts = dialogs.GuessOptionsDialog(
            self, self.config.getint("Security", "bruteforce_max_len", fallback=5)).run()
        if opts is None:
            return
        modes, max_len = opts
        self._run_guessing_process(modes, max_len, items=items)

    def _run_guessing_process(self, modes, max_len, items=None) -> None:
        items = items or self._encrypted_iids()
        if not items:
            qt_util.info(self, _("Gæt kodeord"),
                         _("Der er ingen krypterede PDF-filer på listen."))
            return
        dlg = self._open_guess_dialog(_("Gæt / Tjek kodeord"), items)
        qt_util.run_in_thread(self._guess_passwords, items, modes, max_len,
                              self._get_all_passwords(), dlg, name="pw-guess")

    def _guess_passwords(self, items, modes, max_len, known_passwords, dlg) -> None:
        known = list(known_passwords or [])
        stop = dlg.stop_event
        for iid in items:
            if stop.is_set():
                break
            self._queue.put((dlg.start_row, (iid,)))
            found_pw = None

            # Kendte kodeord foerst (stop-bevidst).
            for pw in known:
                if stop.is_set():
                    break
                if pdf_utils.open_with_passwords(self.paths[iid], [pw]):
                    found_pw = pw
                    break

            if not found_pw and not stop.is_set() and modes.get("numeric"):
                self._queue.put((dlg.set_row_status,
                                 (iid, _("Numerisk (max %s)...") % max_len)))
                found_pw = password_guesser.guess_password(
                    self.paths[iid], max_len=max_len, ui_stop_flag=stop)

            self._report_guess_result(dlg, iid, found_pw, known)
            if not stop.is_set():
                time.sleep(0.05)
        self._queue.put((dlg.mark_finished, ()))
        self._queue.put((self._refresh_all, ()))

    # ------------------------------------------------------------------
    # Indstillinger, log og sprogskift
    # ------------------------------------------------------------------
    def _open_settings_window(self) -> None:
        dlg = dialogs.SettingsDialog(self, self)
        dlg.exec()
        if dlg.language_changed:
            new_lang = self.config.get("General", "language", fallback="en")
            self.language_code = new_lang
            LocalizationManager.get_instance().set_language(new_lang)
            self._rebuild_ui_for_language_change()

    def download_log(self, parent=None) -> None:
        """Gem en kopi af logfilen et sted brugeren vaelger."""
        import shutil
        from .logging_config import get_log_path

        log_path = get_log_path()
        if not log_path.exists():
            qt_util.info(parent or self, _("Log"), _("Der er ingen logfil endnu."))
            return
        dest, _sel = QFileDialog.getSaveFileName(
            parent or self, _("Gem logfil"), "unitedocs.log",
            ";;".join([qt_util.name_filter(_("Logfiler"), "*.log"),
                       qt_util.name_filter(_("Alle filer"), "*.*")]))
        if not dest:
            return
        try:
            shutil.copy2(log_path, dest)
        except Exception as e:
            logger.error("Kunne ikke gemme logfil: %s", e)
            qt_util.error(parent or self, _("Fejl"),
                          _("Kunne ikke gemme logfilen: %s") % e)

    def email_log(self, parent=None) -> None:
        """Aabn mailklienten forudfyldt med de nyeste log-linjer.

        Vedhaeftning er ikke mulig via ``mailto``, og Windows afkorter lange
        mailto-links; derfor indlejres kun de sidste linjer af loggen."""
        from urllib.parse import quote
        from .logging_config import get_log_path

        log_path = get_log_path()
        log_tail = _("(ingen logfil endnu)")
        try:
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="replace").rstrip()
                tail = text[-1000:]
                if len(text) > 1000:
                    tail = tail[tail.find("\n") + 1:]     # undgaa at starte midt i en linje
                if tail.strip():
                    log_tail = tail
        except Exception as e:
            logger.warning("Kunne ikke læse logfil til e-mail: %s", e)

        subject = _("UniteDocs log - version %s") % __version__
        body = (_("Beskriv venligst problemet her:") + "\n\n\n"
                + _("--- Seneste log ---") + "\n" + log_tail)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        url = QUrl("mailto:kontakt@uniteapps.dk?subject=%s&body=%s"
                   % (quote(subject), quote(body)))
        if not QDesktopServices.openUrl(url):
            logger.error("Kunne ikke åbne mailklient for %s", url.toString())
            qt_util.error(parent or self, _("Fejl"),
                          _("Kunne ikke åbne e-mailprogrammet: %s") % url.toString())

    def _rebuild_ui_for_language_change(self) -> None:
        """Byg fladen om, saa den afspejler det nye sprog.

        Modellen, undo-stakken og kodeordslisten er app-tilstand og overlever;
        kun widgets rives ned. ``setCentralWidget`` sletter den gamle flade for
        os, saa 8.x' manuelle ``winfo_children()``-nedrivning er vaek.
        """
        global _
        _ = LocalizationManager.get_text
        Tooltip.hide_all()
        self._status_timer.stop()
        self._update_timer.stop()
        self._close_sort_panel()

        # Aabne ikke-modale dialoger kan ikke overleve en sprogomlaegning.
        for attr in ("_pw_dialog", "_insert_dialog"):
            dlg = getattr(self, attr, None)
            if dlg is not None:
                dlg.close()
            setattr(self, attr, None)

        self.undo_stack.unsubscribe(self._update_undo_buttons)
        self._build_ui()
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()

    # ==================================================================
    # Automatisk anonymisering
    # ==================================================================
    # Flowet spejler kodeordsgaetningen: valgdialog -> snapshot paa UI-traaden
    # -> fremdriftsdialog -> arbejdstraad -> resultat tilbage gennem ``_queue``.
    # Forskellen er at anonymisering er en **session**: har brugeren foerst
    # anonymiseret, maa en fil der tilfoejes bagefter ikke smutte umaskeret med.

    def _anon_tip(self) -> str:
        ok, why = pii.availability()
        if not ok:
            return why
        return _("Find og masker CPR-numre, navne og adresser automatisk")

    def _refresh_anon_button(self) -> None:
        btn = getattr(self, "_anon_btn", None)
        if btn is not None:
            btn.setEnabled(pii.availability()[0])

    def _run_ocr(self) -> None:
        """Kommandobarens Tekstgenkendelse. Selve koerslen ligger i fremviseren,
        som ejer ordlisterne markeringen bruger."""
        if self.page_view is not None:
            self.page_view.run_ocr()

    def _start_anonymize(self) -> None:
        ok, why = pii.availability()
        if not ok:
            qt_util.info(self, _("Automatisk anonymisering"), why)
            return
        if not self.model.files:
            qt_util.info(self, _("Automatisk anonymisering"),
                         _("Tilføj først en eller flere filer."))
            return

        self._ensure_pages_then_rebuild()
        session = self._anon_session
        all_pages = [p.uid for _f, p in self.model.flatten()]
        new_pages = session.unscanned(self.model)
        if not all_pages:
            qt_util.info(self, _("Automatisk anonymisering"),
                         _("Der er ingen sider at gennemgå."))
            return

        opts = dialogs.AnonymizeOptionsDialog(
            self, total_pages=len(all_pages), new_pages=len(new_pages)).run()
        if opts is None:
            return

        # Et manuelt tryk GENOPTAGER forespoergslen om nye filer. Har brugeren
        # sagt "ikke flere filer", er det her vejen tilbage.
        session.prompt_on_new_files = True

        uids = new_pages if opts["scope"] == "new" else all_pages
        if not uids:
            qt_util.info(self, _("Automatisk anonymisering"),
                         _("Der er ingen nye sider at gennemgå."))
            return
        self._run_anonymize(uids, allow_ocr=opts["ocr"])

    def _run_anonymize(self, page_uids, *, allow_ocr=True, quiet=False) -> None:
        """Start en scanning af ``page_uids``. ``quiet`` = ingen dialog uden fund."""
        jobs = anonymize.build_jobs(self.model, only_uids=page_uids)
        if not jobs:
            return
        prog = qt_util.ProgressDialog(
            self, _("Automatisk anonymisering"),
            _("Forbereder gennemgang…"), cancellable=True)
        prog.set_busy()
        prog.show()
        self._anon_progress_at = 0.0
        qt_util.run_in_thread(self._anonymize_worker, jobs,
                              self._get_all_passwords(), allow_ocr, prog, quiet,
                              name="anonymize")

    def _anonymize_worker(self, jobs, pw, allow_ocr, prog, quiet) -> None:
        """**Arbejdstraad.** Al UI-kontakt gaar gennem ``self._queue``."""
        try:
            self._queue.put((prog.set_status, (_("Indlæser sprogmodel…"),)))
            pii.warmup()
            result = anonymize.scan(
                jobs, pw, allow_ocr=allow_ocr,
                on_progress=lambda done, total, label:
                    self._anon_progress(prog, done, total, label),
                cancel=prog.cancel_event)
            self._queue.put((self._anonymize_done,
                             ([j[0] for j in jobs], result, prog, quiet)))
        except Exception as exc:
            logger.exception("Anonymiseringen fejlede")
            self._queue.put((self._anonymize_failed, (prog, exc)))

    def _anon_progress(self, prog, done, total, label) -> None:
        """Kaldes fra arbejdstraaden. Struber, saa UI'en ikke drukner i signaler."""
        now = time.monotonic()
        if done < total and now - self._anon_progress_at < 0.12:
            return
        self._anon_progress_at = now
        self._queue.put((prog.set_page_progress, (done, total)))
        self._queue.put((prog.set_status, (
            _("Side %(nr)d af %(alle)d · %(navn)s")
            % {"nr": done, "alle": total, "navn": label},)))

    def _anonymize_failed(self, prog, exc) -> None:
        prog.finish()
        if isinstance(exc, pii.ModelMissing):
            qt_util.warn(self, _("Automatisk anonymisering"), str(exc))
        else:
            qt_util.error(self, _("Automatisk anonymisering"),
                          _("Gennemgangen fejlede: %s") % exc)

    def _anonymize_done(self, scanned_uids, result, prog, quiet) -> None:
        prog.finish()
        session = self._anon_session
        session.has_run = True
        # Siderne er set — ogsaa dem uden fund. Ellers ville de blive foreslaaet
        # igen ved hver eneste "kun nye sider"-koersel.
        session.mark_scanned(scanned_uids)

        if result.cancelled and not result.findings:
            self.set_status(_("Gennemgangen blev afbrudt"), transient_ms=6000,
                            kind="warning")
            return
        if not result.findings:
            msg = _("Der blev ikke fundet personoplysninger.")
            if quiet:
                self.set_status(msg, transient_ms=8000, kind="success")
            else:
                qt_util.info(self, _("Automatisk anonymisering"), msg)
            return

        groups = anonymize.group_findings(result.findings)
        selected = dialogs.AnonymizeReviewDialog(
            self, groups, result,
            preselected=anonymize.preselect(groups, session.decisions),
            remembered=anonymize.remembered_keys(groups, session.decisions)).run()
        if selected is None:
            self.set_status(_("Anonymiseringen blev annulleret"),
                            transient_ms=6000)
            return

        session.remember(groups, selected)
        items = anonymize.build_items(result.findings, selected, self.model)
        if not items:
            self.set_status(_("Ingen nye maskeringer at tilføje"),
                            transient_ms=6000)
            return

        self.undo_stack.push(em.add_annotations_batch_cmd(
            self.model, items, title=_("Automatisk anonymisering")))
        if self.page_view is not None:
            self.page_view.pcanvas.redraw_overlays()
        self.set_status(_("%d maskeringer tilføjet") % len(items),
                        transient_ms=9000, kind="success")

    # -- nye filer efter en koersel ------------------------------------
    def _maybe_anonymize_new_files(self) -> None:
        """Spoerg om friskt tilfoejede filer ogsaa skal anonymiseres.

        Kaldes naar sider er blevet populeret. Uden det ville en fil tilfoejet
        efter gennemgangen glide umaskeret med i det flettede output — den
        farligste enkeltfejl i hele funktionen.
        """
        session = self._anon_session
        if not session.has_run or not session.prompt_on_new_files:
            return
        if getattr(self, "_anon_asking", False):
            return
        new_uids = session.unscanned(self.model)
        if not new_uids:
            return

        names = []
        for entry in self.model.files:
            if any(p.uid in set(new_uids) for p in entry.pages):
                names.append(os.path.basename(entry.path))
        if not names:
            return

        if len(names) == 1:
            text = _("\"%s\" er tilføjet efter at dokumentet blev anonymiseret. "
                     "Skal den også gennemgås?") % names[0]
        else:
            text = _("%d filer er tilføjet efter at dokumentet blev "
                     "anonymiseret. Skal de også gennemgås?") % len(names)

        self._anon_asking = True
        try:
            choice = qt_util.ask_choice(
                self, _("Automatisk anonymisering"), text,
                [("scan", _("Anonymisér")),
                 ("skip", _("Ikke denne fil") if len(names) == 1
                  else _("Ikke disse filer")),
                 ("never", _("Ikke flere filer"))])
        finally:
            self._anon_asking = False

        if choice == "scan":
            self._run_anonymize(new_uids, quiet=True)
            return
        if choice == "never":
            session.prompt_on_new_files = False
        if choice in ("skip", "never"):
            # Markér som set, saa der ikke spoerges om de samme filer igen.
            session.mark_scanned(new_uids)

    # ------------------------------------------------------------------
    # Kopiér tekst til udklipsholderen
    # ------------------------------------------------------------------
    def _copy_to_clipboard(self) -> None:
        """Kommandobarens knap: hele dokumentet, eller markeringen hvis der er en."""
        self.copy_to_clipboard(None)

    def copy_to_clipboard(self, page_uids=None) -> None:
        """Kopiér tekst fra ``page_uids`` (eller markeringen/hele dokumentet).

        Tekstudtraekket koerer altid tekstgenkendelse paa sider uden tekstlag --
        uden den ville en scanning blive kopieret som ingenting, og det ville
        ligne at funktionen var i stykker.
        """
        if not self.model.files:
            qt_util.warn(self, _("Ingen filer"), _("Tilføj filer først"))
            return
        self._ensure_all_unlocked()
        only = set(page_uids) if page_uids else self._page_scope()
        jobs = pseudonymize.build_jobs(self.model, only)
        if not jobs:
            qt_util.warn(self, _("Ingen sider"), _("Der er ingen sider at kopiere."))
            return

        ok_pii, _grund = pii.availability()
        opts = dialogs.ClipboardOptionsDialog(
            self, page_count=len(jobs), pii_available=ok_pii).run()
        if not opts:
            return

        self._clip_sources = pseudonymize.source_names(self.model, only)
        prog = qt_util.ProgressDialog(
            self, _("Kopierer tekst"),
            _("Læser %d side(r)…") % len(jobs), cancellable=True)
        prog.show()
        self.set_status(_("Læser tekst…"))
        qt_util.run_in_thread(self._clipboard_worker, jobs,
                              self._get_all_passwords(), opts, prog,
                              name="clipboard-text")

    def _clipboard_worker(self, jobs, pw, opts, prog) -> None:
        ev = prog.cancel_event
        try:
            result = pseudonymize.scan(
                jobs, pw, allow_ocr=True, analyze=opts["pseudonymize"],
                on_progress=lambda d, t, lbl: self._queue.put(
                    (self._clip_progress, (prog, d, t, lbl))),
                cancel=ev)
            self._queue.put((self._clipboard_done, (result, opts, prog)))
        except pii.ModelMissing as e:
            self._queue.put((self._clipboard_failed, (prog, e, True)))
        except Exception as e:
            logger.error("Tekstkopiering fejlede: %s", e)
            self._queue.put((self._clipboard_failed, (prog, e, False)))

    def _clip_progress(self, prog, done, total, label) -> None:
        prog.set_fraction(done, total)
        prog.set_status(_("Læser %s…") % label)

    def _clipboard_failed(self, prog, exc, model_missing) -> None:
        prog.finish()
        self.set_status()
        if model_missing:
            qt_util.error(self, _("Pseudonymisering"),
                          _("Sprogmodellen mangler, så teksten kan ikke "
                            "pseudonymiseres.\n\n%s") % exc)
        else:
            qt_util.error(self, _("Kopiering fejlede"), str(exc))

    def _clipboard_done(self, result, opts, prog) -> None:
        prog.finish()
        self.set_status()
        if result.cancelled:
            qt_util.info(self, _("Annulleret"), _("Kopieringen blev annulleret."))
            return
        if not result.pages:
            qt_util.warn(self, _("Ingen tekst"),
                         _("Der blev ikke fundet tekst på de valgte sider."))
            return

        pseudonyms, selected = (), None
        if opts["pseudonymize"]:
            groups = anonymize.group_findings(result.findings)
            if opts["review"] and groups:
                dlg = dialogs.AnonymizeReviewDialog(
                    self, groups, result,
                    intro=_("Alt er valgt. Fjern fluebenet ved det der IKKE "
                            "skal byttes ud med et pseudonym."),
                    ok_label=_("Kopiér med pseudonymer"),
                    counter_fmt=_("%(valgt)d af %(alle)d forekomster byttes ud"))
                selected = dlg.run()
                if selected is None:
                    return
            else:
                selected = {f.uid for f in result.findings}
            pseudonyms = pseudonymize.assign(result.findings, selected)

        text = pseudonymize.render(result, pseudonyms, selected)
        QApplication.clipboard().setText(text)
        self._last_pseudonyms = list(pseudonyms)
        dialogs.ClipboardDoneDialog(
            self, chars=len(text), pages=len(result.pages),
            pseudonyms=len(pseudonyms), pseudonymized=bool(opts["pseudonymize"]),
            ocred=result.pages_ocred, failed=result.pages_failed,
            save_overview=self._save_pseudonym_overview).exec()
        self.set_status(_("Teksten er kopieret til udklipsholderen."),
                        transient_ms=6000)

    def _save_pseudonym_overview(self) -> None:
        """Gem navneoversigten. Kaldes fra kvitteringsdialogen.

        Filen er den ENESTE forbindelse mellem pseudonym og original -- der er
        ingen vej tilbage fra den kopierede tekst -- og dens foerste linje siger
        derfor selv at den ikke maa uploades.
        """
        pseudonyms = getattr(self, "_last_pseudonyms", None)
        if not pseudonyms:
            qt_util.info(self, _("Navneoversigt"),
                         _("Der er ingen pseudonymer at gemme."))
            return
        fmt = qt_util.ask_choice(
            self, _("Gem navneoversigt"), _("Hvilket format?"),
            [("pdf", "PDF"), ("md", _("Markdown")), ("txt", _("Tekstfil"))])
        if not fmt:
            return
        ext = "." + fmt
        out_path, _sel = QFileDialog.getSaveFileName(
            self, _("Gem navneoversigt"), _("Pseudonymer") + ext,
            qt_util.name_filter(fmt.upper(), "*" + ext))
        if not out_path:
            return
        sources = getattr(self, "_clip_sources", ())
        try:
            if fmt == "pdf":
                pseudonymize.write_overview_pdf(out_path, pseudonyms,
                                                sources=sources)
            else:
                Path(out_path).write_text(
                    pseudonymize.overview_text(pseudonyms, fmt=fmt,
                                               sources=sources),
                    encoding="utf-8")
        except Exception as e:
            logger.error("Kunne ikke gemme navneoversigten: %s", e)
            qt_util.error(self, _("Gem navneoversigt"), str(e))
            return
        self._show_saved_dialog(_("Navneoversigt"), _("Oversigten er gemt som:"),
                                Path(out_path), is_folder=False)
