import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import os
import sys
import queue
import subprocess
import threading
import uuid
import datetime
import time
from PIL import Image, ImageTk
import pymupdf
from . import pdf_renderer  # Rendering via PyMuPDF
from . import export_formats
from . import pdf_utils
from . import utils
from . import edit_model as em
from . import save_pipeline
from . import page_render
from .page_view import PageView
from .undo_stack import UndoStack, Command
from . import annotations
from . import password_guesser
from .config import AppConfig
from . import __version__
from .localization import LocalizationManager
from . import context_menu
from . import updater
from . import theme
from . import icons_vector
from .tooltip import Tooltip
from .logging_config import get_logger

logger = get_logger(__name__)
import gettext

# INTERNATIONALIZATION: Global translation function
# AI ASSISTANTS: Always use _("text") for user-visible strings
# Example: ttk.Label(parent, text=_("Save file"))
# After adding new _("strings"), run: python update_locales.py
_ = LocalizationManager.get_text

try:
    import tkinterdnd2 as _tkdnd_pkg
    from tkinterdnd2 import DND_FILES as _DND_FILES
    _tkdnd_support = True
except ImportError:
    _tkdnd_support = False


class PDFTool(tk.Tk):
    def __init__(self, initial_files: list[str] | None = None):
        self.config = AppConfig()
        self.language_code = self.config.get("General", "language", fallback="en")

        # Initialize Localization Manager (Fix for Bug 1)
        LocalizationManager.initialize(self.language_code)

        super().__init__()

        if _tkdnd_support:
            try:
                _tkdnd_pkg.TkinterDnD._require(self)
            except Exception as e:
                logger.warning("tkdnd init failed: %s", e)

        # Windows 11-chrome via sv-ttk (eneste tema-motor). Saettes EFTER
        # super().__init__(), inden nogen widgets bygges.
        self.style = theme.apply_theme(self)

        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.bind_all("<Escape>", self._on_escape)
        # Undo/redo. Guarded so Text widgets (kodeord / fritekst-felter) keep
        # their own editing undo.
        self.bind_all("<Control-z>", self._do_undo)
        self.bind_all("<Control-Z>", self._do_undo)
        self.bind_all("<Control-y>", self._do_redo)
        self.bind_all("<Control-Shift-Z>", self._do_redo)
        self.bind_all("<Control-Shift-z>", self._do_redo)
        # Sætter ikonet på dette vindue og på alle Toplevel-vinduer der åbnes
        # senere (dialoger, PreviewWindow, kodeordsvinduer) — se
        # utils.install_window_icon for hvorfor default= alene ikke rækker.
        utils.install_window_icon(self)

        self.title(f"Unite Docs – v{__version__}")
        # Bred nok til sidegitteret + fremviseren med hele annotationslinjen.
        self.geometry("1200x820")
        self.minsize(1000, 560)

        # Internal state containers.
        # EditModel is the single source of truth for the file list; the Treeview
        # is a rendered view of it. `self.paths` is a read-only property derived
        # from the model (see below) so external readers keep working. rotations/
        # croppings stay real dicts in this phase (preview_window writes to them);
        # they migrate to per-page state when the page view lands.
        self.model = em.EditModel()

        # Password list is now app state (the bottom "Adgangskoder" panel was
        # replaced by an on-demand prompt + a command-bar dialog). This is the
        # single source of truth for GUI-entered passwords; it survives a
        # language rebuild because it is not a widget.
        self._pw_lines: list[str] = []
        self._pw_dialog = None            # open password-manager dialog (if any)
        # "Indsæt side": genererede sider skrives til en session-mappe under
        # %APPDATA% og lever der indtil appen lukkes (modellen peger på dem).
        self._insert_dialog = None
        self._inserted_dir = None         # oprettes dovent ved første indsættelse
        # On-open/on-add unlock prompting for encrypted files.
        self._prompt_on_metadata: set[str] = set()   # iids to prompt once metadata lands
        self._pending_unlock: list[str] = []          # queued iids awaiting a modal prompt
        self._unlocking = False                        # re-entrancy guard for the queue
        self._status_after = None                      # transient-status reset handle
        # Performance: in-memory cache for the combined GUI + disk password list.
        # Invalidated by _save_password_to_cache() whenever a new password is persisted.
        self._pw_cache: list[str] | None = None

        # Efterladte "Indsæt side"-mapper fra en crashet kørsel ryddes her.
        utils.purge_old_inserted_dirs()

        # Background renderer for the page view (its own worker pool).
        self.page_render_mgr = page_render.PageRenderManager(self)

        # Undo/redo (Fase 5). The stack mutates the model via inverse
        # closures; the app resyncs its views after each undo/redo. Buttons
        # subscribe for enable/disable.
        self.undo_stack = UndoStack()

        self.page_view = None

        self._stop_guessing = False
        # Sorterings-retning pr. noegle. "Omvendt" er tilstandsloes.
        self._sort_dir = {em.SORT_DATE: False, em.SORT_NAME: False, em.SORT_SIZE: False}
        self._sort_panel = None            # persistent Sorter-panel (Toplevel)
        # Autoupdater: after()-id for opstartstjekket (annulleres ved sprogskift)
        # og en vagt, så to tjek aldrig kører samtidig.
        self._update_after_id = None
        self._update_busy = False

        # Build UI + async queue loop
        self._load_icons()
        self._build_ui()
        self._queue = queue.Queue()
        self.after(50, self._process_queue)

        self.page_render_mgr.start()
        self.after(30000, self._periodic_cleanup)

        # --- Dynamic Resizing (Language Support) ---
        # Adjust window width if translated buttons require more space
        self.update_idletasks()
        req_width = self.toolbar.winfo_reqwidth() + 60  # Add padding for safety
        if req_width > 1200:
            self.geometry(f"{req_width}x820")
            self.minsize(req_width, 560)

        if initial_files:
            self.after(300, lambda: self._add_paths(initial_files))

        self.after(200, self._start_ipc)

        # Opdateringstjekket skal ikke kappes om CPU'en med første optegning,
        # IPC-starten eller indlæsningen af initial_files. 8 sekunder er rigeligt.
        self._update_after_id = self.after(8000, self._start_update_check)

    def _load_icons(self):
        """Byg ikon-adgangen. Ikonerne tegnes i kode (``icons_vector``); den gamle
        ``client/icons/*.png``-mappe er væk sammen med filvisningen.

        Kaldes to gange: fra ``__init__`` og igen fra
        ``_rebuild_ui_for_language_change``. **IconFactory maa kun bygges én
        gang** -- den ejer alle PhotoImages i rootens levetid, og en ny factory
        pr. sprogskift ville lade de gamle billeder hobe sig op.
        """
        if getattr(self, "icon_factory", None) is None:
            self.icon_factory = icons_vector.IconFactory(self)
        self.icons = icons_vector.LazyIconDict(self.icon_factory)

    def _start_ipc(self):
        from . import ipc
        hwnd = self.winfo_id()
        ipc.start_ipc_server(
            schedule_callback=lambda paths: self.after(0, lambda: self._receive_ipc_paths(paths)),
            hwnd=hwnd
        )

    def _receive_ipc_paths(self, paths: list[str]):
        self._add_paths(paths)
        self.lift()
        self.focus_force()

    # ------------------------------------------------------------------
    # Autoupdater. Logikken (HTTP, hash, procesudløsning) ligger i
    # ``updater.py``; her er kun planlægning, trådmarshalling og UI.
    # ------------------------------------------------------------------

    def _updates_dir(self) -> Path:
        return utils.get_app_data_path(updater.DOWNLOAD_SUBDIR)

    def _start_update_check(self, manual: bool = False):
        """Ugentligt (eller manuelt udløst) tjek for en nyere version."""
        self._update_after_id = None
        if self._update_busy:
            if manual:
                self.set_status(_("Søger allerede efter opdateringer…"),
                                transient_ms=4000)
            return
        if not manual:
            if not self.config.getboolean("Updates", "auto_check", fallback=True):
                return
            try:
                last = float(self.config.get("Updates", "last_check",
                                             fallback="0") or 0)
            except (TypeError, ValueError):
                last = 0.0
            if time.time() - last < updater.CHECK_INTERVAL_S:
                return

        # Stemplet sættes FØR tjekket, ikke efter. Ellers ville en netværksfejl
        # betyde et nyt forsøg ved hver eneste opstart i stedet for om en uge.
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
            except Exception as exc:                      # oprydning må aldrig vælte tjekket
                logger.debug("Oprydning i %s fejlede: %s", updates_dir, exc)
            try:
                info, err = updater.check(current), None
            except Exception as exc:
                info, err = None, exc
            self._queue.put((self._on_update_check_result, (info, manual, err)))

        threading.Thread(target=worker, daemon=True, name="update-check").start()

    def _on_update_check_result(self, info, manual, err=None):
        """Kører på hovedtråden via ``_process_queue``."""
        self._update_busy = False
        if not self.winfo_exists():
            return
        if err is not None:
            logger.warning("Opdateringstjek mislykkedes: %s", err)
            if manual:
                self._show_custom_dialog(
                    _("Opdatering"),
                    _("Kunne ikke kontakte serveren:\n%s") % err, "error")
            return
        if info is None or not info.update_available:
            if manual:
                self._show_custom_dialog(
                    _("Opdatering"),
                    _("Du kører allerede den nyeste version (%s).") % __version__)
            return

        skipped = (self.config.get("Updates", "skipped_version", fallback="") or "").strip()
        if not manual and skipped == info.version:
            logger.info("Version %s er sprunget over af brugeren.", info.version)
            return

        choice = self._show_update_dialog(info)
        if choice == "skip":
            self.config.set("Updates", "skipped_version", info.version)
            self.config.save()
            self.set_status(_("Version %s springes over.") % info.version,
                            transient_ms=6000)
        elif choice == "install":
            self._run_update_download(info)

    def _show_update_dialog(self, info) -> str:
        """Modal dialog. Returnerer ``"install"``, ``"later"`` eller ``"skip"``."""
        dialog = tk.Toplevel(self)
        dialog.title(_("Opdatering tilgængelig"))
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.configure(background=theme.C["bg"])

        result = {"v": "later"}
        body = ttk.Frame(dialog, padding=(18, 14))
        body.pack(fill="both", expand=True)

        ttk.Label(body, text=_("Der er en ny version af Unite Docs"),
                  font=theme.FONTS["title"]).pack(anchor="w")
        ttk.Label(body,
                  text=_("Version %s er klar. Du kører version %s.")
                       % (info.version, __version__),
                  wraplength=460, justify="left").pack(anchor="w", pady=(6, 0))
        if info.size:
            ttk.Label(body, text=_("Download: %s") % self._fmt_bytes(info.size),
                      foreground=theme.C["text_muted"],
                      font=theme.FONTS["small"]).pack(anchor="w", pady=(2, 0))

        if info.release_notes:
            ttk.Label(body, text=_("Nyt i denne version:"),
                      font=theme.FONTS["strong"]).pack(anchor="w", pady=(12, 4))
            notes_wrap = ttk.Frame(body)
            notes_wrap.pack(fill="both", expand=True)
            notes = tk.Text(notes_wrap, height=9, width=58, wrap="word")
            scroll = ttk.Scrollbar(notes_wrap, orient="vertical", command=notes.yview)
            notes.configure(yscrollcommand=scroll.set)
            theme.style_text(notes)
            notes.insert("1.0", info.release_notes)
            notes.configure(state="disabled")
            scroll.pack(side="right", fill="y")
            notes.pack(side="left", fill="both", expand=True)

        ttk.Separator(dialog, orient="horizontal").pack(fill="x")
        bar = ttk.Frame(dialog, padding=(18, 10))
        bar.pack(fill="x")

        def choose(value):
            result["v"] = value
            dialog.destroy()

        ttk.Button(bar, text=_("Opdater nu"), style="Accent.TButton",
                   command=lambda: choose("install")).pack(side="right")
        ttk.Button(bar, text=_("Ikke nu"),
                   command=lambda: choose("later")).pack(side="right", padx=(0, 6))
        ttk.Button(bar, text=_("Spring denne version over"),
                   command=lambda: choose("skip")).pack(side="left")

        dialog.bind("<Escape>", lambda _e: choose("later"))
        dialog.protocol("WM_DELETE_WINDOW", lambda: choose("later"))
        dialog.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dialog.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dialog.winfo_height()) // 3
        dialog.geometry("+%d+%d" % (max(0, x), max(0, y)))
        dialog.grab_set()
        dialog.wait_window()
        return result["v"]

    def _run_update_download(self, info):
        p = self._show_progress_dialog(
            _("Henter opdatering"),
            _("Henter Unite Docs %s…") % info.version, cancellable=True)
        dest = self._updates_dir()
        cancel = p["cancel"]

        def worker():
            try:
                path = updater.download(
                    info, dest,
                    progress_cb=lambda done, total: self._queue.put(
                        (self._update_download_progress, (p, done, total))),
                    cancel_event=cancel)
            except Exception as exc:
                self._queue.put((self._update_failed, (p, exc, info)))
                return
            self._queue.put((self._finish_update, (p, path, info)))

        threading.Thread(target=worker, daemon=True, name="update-download").start()

    def _update_download_progress(self, p_widgets, done, total):
        if not p_widgets["window"].winfo_exists():
            return
        if total:
            p_widgets["bar"].config(mode="determinate", maximum=total, value=done)
            p_widgets["label"].config(
                text=_("Hentet %s af %s")
                     % (self._fmt_bytes(done), self._fmt_bytes(total)))
        else:
            p_widgets["label"].config(text=_("Hentet %s") % self._fmt_bytes(done))

    def _finish_update(self, p_widgets, path, info):
        self._destroy_progress(p_widgets)
        if not self.winfo_exists():
            return
        if not updater.can_install():
            # Kører fra kildekode: en installer ville skrive et helt andet sted
            # end den kørende app. Peg brugeren mod hjemmesiden i stedet.
            self._show_update_link_dialog(
                _("Opdatering hentet"),
                _("Installationen kan kun køres fra en installeret udgave. "
                  "Filen ligger her, og hentesiden er åbnet i browseren."),
                info, extra_path=path)
            return

        self._show_custom_dialog(
            _("Installerer opdatering"),
            _("Unite Docs lukker nu og åbner igen, når version %s er "
              "installeret.") % info.version)
        try:
            updater.launch_installer(path, Path(sys.executable))
        except Exception as exc:
            logger.error("Kunne ikke starte installeren: %s", exc)
            self._show_update_link_dialog(
                _("Installationen kunne ikke startes"),
                _("Filen blev hentet, men installeren kunne ikke startes:\n%s")
                % exc, info, extra_path=path)
            return
        self._on_closing()

    def _update_failed(self, p_widgets, exc, info):
        self._destroy_progress(p_widgets)
        if not self.winfo_exists():
            return
        if isinstance(exc, updater.UpdateCancelled):
            self.set_status(_("Download afbrudt."), transient_ms=5000)
            return
        logger.error("Download af opdatering fejlede: %s", exc)
        self._show_update_link_dialog(
            _("Opdateringen kunne ikke hentes"),
            _("Du kan hente installeren manuelt fra hjemmesiden.\n\n%s") % exc,
            info)

    def _show_update_link_dialog(self, title, message, info, extra_path=None):
        """Fejl-/fallback-dialog med et klikbart link til hentesiden."""
        import webbrowser

        url = getattr(info, "info_url", None) or updater.INFO_URL
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.resizable(False, False)
        body = ttk.Frame(dlg, padding=(18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, wraplength=440, justify="left").pack(anchor="w")

        link = ttk.Label(body, text=url, foreground=theme.C["link"], cursor="hand2",
                         font=theme.FONTS["link"], wraplength=440, justify="left")
        link.pack(anchor="w", pady=(10, 0))
        link.bind("<Button-1>", lambda _e: webbrowser.open(url))

        if extra_path is not None:
            file_link = ttk.Label(body, text=str(extra_path),
                                  foreground=theme.C["link"], cursor="hand2",
                                  font=theme.FONTS["link"], wraplength=440,
                                  justify="left")
            file_link.pack(anchor="w", pady=(4, 0))
            file_link.bind("<Button-1>",
                           lambda _e: self._open_in_explorer(Path(extra_path), select=True))

        ttk.Separator(dlg, orient="horizontal").pack(fill="x")
        bar = ttk.Frame(dlg, padding=(18, 10))
        bar.pack(fill="x")
        ok = ttk.Button(bar, text=_("Luk"), style="Accent.TButton", command=dlg.destroy)
        ok.pack(side="right")
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.bind("<Return>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry("+%d+%d" % (max(0, x), max(0, y)))
        ok.focus_set()
        webbrowser.open(url)

    def _on_closing(self):
        try:
            from . import ipc
            ipc.cleanup_ipc()
        except Exception:
            pass
        try:
            utils.purge_stale_temp_files(force_all=True)
            if hasattr(self, 'page_render_mgr'):
                self.page_render_mgr.stop()
            pdf_renderer.clear_doc_cache()
            if getattr(self, '_inserted_dir', None) is not None:
                utils._force_rmtree(self._inserted_dir)
        finally:
            self.destroy()

    # _cleanup_temp_files moved to ThumbnailManager
    
    def _on_escape(self, event=None):
        """Escape: afbryd et igangvaerende sidetraek og luk Sorter-panelet."""
        if self.page_view is not None:
            self.page_view.cancel_drag()
        self._close_sort_panel()

    def _periodic_cleanup(self):
        """Ryd gamle midlertidige filer og planlaeg naeste koersel."""
        utils.purge_stale_temp_files()
        self.after(30000, self._periodic_cleanup)

    def _get_password_cache_path(self) -> Path:
        return utils.get_app_data_path() / "password_cache.txt"

    def _load_cached_passwords(self) -> list[str]:
        cache_path = self._get_password_cache_path()
        if not cache_path.exists():
            return []
        try:
            with open(cache_path, 'r', encoding='utf-8') as f:
                return [line.strip() for line in f if line.strip()]
        except (OSError, IOError) as e:
            logger.error("Fejl ved indlæsning af adgangskode-cache: %s", e)
            return []

    def _save_password_to_cache(self, password: str):
        if not password:
            return
        cache_path = self._get_password_cache_path()
        try:
            existing_passwords = set(self._load_cached_passwords())
            if password not in existing_passwords:
                with open(cache_path, 'a', encoding='utf-8') as f:
                    f.write(password + '\n')
                # Performance: invalidate in-memory cache so next call re-reads from disk
                self._pw_cache = None
        except (OSError, IOError) as e:
            logger.error("Fejl ved lagring af adgangskode til cache: %s", e)

    def _get_all_passwords(self) -> list[str]:
        # Performance: return cached list if available; avoids repeated disk reads.
        # Cache is invalidated by _save_password_to_cache() when new passwords are stored.
        if self._pw_cache is not None:
            return self._pw_cache
        gui_passwords = self._pwlist()
        cached_passwords = self._load_cached_passwords()
        combined = gui_passwords + cached_passwords
        self._pw_cache = list(dict.fromkeys(combined))
        return self._pw_cache

    def _insert_page_dir(self):
        """Session-mappen til genererede sider (oprettes ved første brug)."""
        if self._inserted_dir is None:
            self._inserted_dir = utils.new_inserted_pages_dir()
        return self._inserted_dir

    @staticmethod
    def _safe_filename(name: str, fallback: str) -> str:
        """Gør en overskrift brugbar som filnavn. Navnet er ikke kosmetik: det
        bliver kapiteltitlen i md/ePub-eksport (``FileJob.title``)."""
        cleaned = "".join(ch for ch in (name or "") if ch not in '\\\\/:*?"<>|').strip()
        cleaned = " ".join(cleaned.split())[:60]
        return cleaned or fallback

    def _open_insert_page_dialog(self):
        """Editor til en ny side: overskrift + fritekst. Ved OK spørges der om
        siden skal ligge før eller efter den markerede side/fil."""
        if getattr(self, "_insert_dialog", None) is not None and self._insert_dialog.winfo_exists():
            self._insert_dialog.deiconify()
            self._insert_dialog.lift()
            return
        dlg = tk.Toplevel(self)
        dlg.title(_("Indsæt side"))
        dlg.transient(self)
        dlg.resizable(True, True)
        self._insert_dialog = dlg

        top = ttk.Frame(dlg, padding=(12, 12, 12, 0))
        top.pack(fill="x")
        ttk.Label(top, text=_("Overskrift")).pack(anchor="w")
        head_var = tk.StringVar()
        head = ttk.Entry(top, textvariable=head_var, width=52)
        head.pack(fill="x", pady=(2, 0))

        body_frame = ttk.Frame(dlg, padding=(12, 10, 12, 0))
        body_frame.pack(fill="both", expand=True)
        ttk.Label(body_frame, text=_("Tekst")).pack(anchor="w")
        box = ttk.Frame(body_frame)
        box.pack(fill="both", expand=True, pady=(2, 0))
        txt = tk.Text(box, width=52, height=14, wrap="word", undo=True)
        theme.style_text(txt)
        sb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)

        def close():
            self._insert_dialog = None
            dlg.destroy()

        def ok():
            heading = head_var.get().strip()
            body = txt.get("1.0", "end-1c")
            if not heading and not body.strip():
                messagebox.showinfo(_("Indsæt side"),
                                    _("Skriv en overskrift eller noget tekst først."),
                                    parent=dlg)
                return
            close()
            self._insert_page(heading, body)

        bar = ttk.Frame(dlg, padding=12)
        bar.pack(fill="x")
        ttk.Button(bar, text=_("Indsæt"), style="Accent.TButton", command=ok).pack(side="right")
        ttk.Button(bar, text=_("Annuller"), command=close).pack(side="right", padx=(0, 6))
        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.bind("<Escape>", lambda e: close())
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (self.winfo_x() + 80, self.winfo_y() + 80))
        head.focus_set()

    def _ask_before_after(self, question: str):
        """Modal dialog: Før / Efter / Annuller. Returnerer "before", "after"
        eller None.

        Bygget som en Toplevel frem for ``messagebox.askquestion``, fordi de
        indbyggede knaptekster ikke kan oversættes gennem vores egen
        ``_()``-kæde."""
        dlg = tk.Toplevel(self)
        dlg.title(_("Indsæt side"))
        dlg.transient(self)
        dlg.resizable(False, False)
        dlg.grab_set()
        result = {"value": None}

        ttk.Label(dlg, text=question, wraplength=380).pack(
            padx=16, pady=(16, 0), anchor="w")

        def choose(value):
            result["value"] = value
            dlg.destroy()

        bar = ttk.Frame(dlg, padding=16)
        bar.pack(fill="x")
        after_btn = ttk.Button(bar, text=_("Efter"), style="Accent.TButton",
                               command=lambda: choose("after"))
        after_btn.pack(side="right")
        ttk.Button(bar, text=_("Før"), command=lambda: choose("before")).pack(
            side="right", padx=(0, 6))
        ttk.Button(bar, text=_("Annuller"), command=lambda: choose(None)).pack(
            side="right", padx=(0, 6))
        dlg.protocol("WM_DELETE_WINDOW", lambda: choose(None))
        dlg.bind("<Escape>", lambda e: choose(None))
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (self.winfo_x() + 120, self.winfo_y() + 140))
        after_btn.focus_set()
        self.wait_window(dlg)
        return result["value"]

    def _build_inserted_pdf(self, heading: str, body: str):
        """Skriv den genererede side til session-mappen. Returnerer (sti, antal
        sider) — en lang fritekst flyder over flere sider — eller (None, 0)."""
        buf = utils.create_text_page_pdf(heading, body)
        if buf is None:
            messagebox.showerror(_("Indsæt side"), _("Siden kunne ikke oprettes."))
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
            messagebox.showerror(_("Indsæt side"), _("Siden kunne ikke oprettes."))
            return None, 0
        return str(path), count

    def _insert_page(self, heading: str, body: str):
        """Byg siden og læg den ind i modellen på den plads brugeren vælger."""
        self._insert_page_in_page_view(heading, body)

    def _insert_page_in_page_view(self, heading: str, body: str):
        uid = getattr(self.page_view, "_selected_uid", None)
        found = self.model.page_by_uid(uid) if uid else None
        if found is None:
            # Ingen markeret side: læg siden sidst i dokumentet.
            return self._insert_page_as_file(heading, body, index=None)
        entry, page = found
        num = self.page_view.page_number_label(uid)
        if num and num.isdigit():
            question = _("Skal siden oprettes før eller efter side %s?") % num
        else:
            question = _("Skal siden oprettes før eller efter den markerede side?")
        where = self._ask_before_after(question)
        if where is None:
            return
        path, count = self._build_inserted_pdf(heading, body)
        if not path:
            return
        # Siderne lægges ind i den markerede sides FIL, men med deres egen
        # src_path — modellen tillader netop det (se edit_model.PageEdit).
        file_index = self.model.index_of_iid(entry.iid)
        pos = entry.pages.index(page) + (1 if where == "after" else 0)
        pages = [em.PageEdit(src_path=path, src_index=i) for i in range(count)]
        self.undo_stack.push(em.insert_pages_cmd(self.model, file_index, pos, pages))
        self.page_view.rebuild()
        self.page_view._select_and_reveal(pages[0].uid)
        self._refresh_status()
        self.set_status(_("Side indsat"), transient_ms=4000, kind="success")

    def _insert_page_as_file(self, heading: str, body: str, index=None):
        """Læg den genererede side ind som sin EGEN fil i listen (filvisning, og
        når der ingen markeret side er)."""
        path, count = self._build_inserted_pdf(heading, body)
        if not path:
            return
        entry = em.FileEntry(
            iid=uuid.uuid4().hex,
            path=path,
            kind=em.KIND_PDF,
            enc_key=pdf_utils.ENC_NOT_ENCRYPTED,
            creation_date="",
            source_page_count=count,
        )
        self.model.populate_pages(entry, count)
        at = len(self.model.files) if index is None else index

        def do():
            self.model.add_file(entry, at)

        def undo():
            self.model.remove_file(entry.iid)

        self.undo_stack.push(Command("Indsæt side", do, undo))
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()
        self.set_status(_("Side indsat"), transient_ms=4000, kind="success")

    def _show_progress_dialog(self, title: str, text: str, cancellable: bool = False) -> dict:
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)
        ttk.Label(win, text=text, font=("Segoe UI", 10)).pack(pady=(10, 0), padx=10)
        status_label = ttk.Label(win, text=_("Initialiserer..."), font=("Segoe UI", 9))
        status_label.pack(pady=5, padx=10, anchor="w")
        bar = ttk.Progressbar(win, mode="determinate", length=380)
        bar.pack(fill="x", padx=20, pady=(0, 6))
        elapsed_label = ttk.Label(win, text="", font=("Segoe UI", 8),
                                  foreground=theme.C["text_muted"])
        elapsed_label.pack(padx=20, anchor="w")
        p = {"window": win, "bar": bar, "label": status_label, "elapsed": elapsed_label,
             "start": time.time(), "cancel": None, "_conv_start": None}

        def tick():
            if not win.winfo_exists():
                return
            elapsed_label.config(text=_("Forløbet: %s") % self._fmt_secs(time.time() - p["start"]))
            win.after(1000, tick)
        tick()

        if cancellable:
            ev = threading.Event()
            p["cancel"] = ev
            cancel_btn = ttk.Button(win, text=_("Annuller"))

            def do_cancel():
                ev.set()
                status_label.config(text=_("Annullerer…"))
                try:
                    cancel_btn.config(state="disabled")
                except tk.TclError:
                    pass
            cancel_btn.config(command=do_cancel)
            cancel_btn.pack(pady=(4, 10))
            win.protocol("WM_DELETE_WINDOW", do_cancel)
        else:
            win.protocol("WM_DELETE_WINDOW", lambda: None)
        win.geometry("440x180")
        win.update()
        return p

    @staticmethod
    def _fmt_bytes(num) -> str:
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

    @staticmethod
    def _fmt_secs(secs) -> str:
        secs = int(secs)
        if secs < 60:
            return "%ds" % secs
        return "%dm %02ds" % (secs // 60, secs % 60)

    def report_callback_exception(self, exc, val, tb):
        """Log undtagelser fra Tk-callbacks (knapper, after(), events).

        Tk sluger normalt disse og skriver dem kun til stderr, som er tom i en
        vinduesbygget exe — så de forsvandt fra loggen. Nu logges de.
        """
        logger.critical("Ufanget undtagelse i Tk-callback", exc_info=(exc, val, tb))

    def _open_in_explorer(self, target: Path, select: bool = False):
        """Aabn en gemt fil (eller dens mappe) i Stifinder.

        ``select=True`` aabner mappen med filen markeret, hvilket er mere
        brugbart end at aabne selve mappen naar man lige har gemt EN fil.
        """
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
        """Kvittering efter gem, med et KLIKBART link til resultatet.

        En almindelig ``messagebox`` kan kun vise stien som tekst; her kan man
        klikke sig direkte hen til filen (eller mappen, naar der blev gemt
        flere enkeltfiler)."""
        target = Path(target)
        dlg = tk.Toplevel(self)
        dlg.title(title)
        dlg.transient(self)
        dlg.resizable(False, False)
        utils.install_window_icon(dlg)
        body = ttk.Frame(dlg, padding=(18, 14))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=message, justify="left").pack(anchor="w")

        link_text = target.name if not is_folder else str(target)
        link = ttk.Label(body, text=link_text, foreground=theme.C["link"],
                         cursor="hand2", font=theme.FONTS["link"], wraplength=460,
                         justify="left")
        link.pack(anchor="w", pady=(8, 0))
        link.bind("<Button-1>",
                  lambda _e: self._open_in_explorer(target, select=not is_folder))

        hint = _("Klik for at åbne mappen") if is_folder else _("Klik for at åbne filen")
        ttk.Label(body, text=hint, foreground=theme.C["text_muted"],
                  font=theme.FONTS["small"]).pack(anchor="w")

        ttk.Separator(dlg, orient="horizontal").pack(fill="x")
        bar = ttk.Frame(dlg, padding=(18, 10))
        bar.pack(fill="x")
        ok = ttk.Button(bar, text=_("Luk"), style="Accent.TButton",
                        command=dlg.destroy)
        ok.pack(side="right")
        dlg.bind("<Escape>", lambda _e: dlg.destroy())
        dlg.bind("<Return>", lambda _e: dlg.destroy())
        dlg.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - dlg.winfo_width()) // 2
        y = self.winfo_rooty() + (self.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry("+%d+%d" % (max(0, x), max(0, y)))
        ok.focus_set()
        return dlg

    def _show_custom_dialog(self, title, message, dialog_type="info", parent_window=None):
        """Shows a custom dialog positioned relative to parent window"""
        # Create dialog window
        dialog = tk.Toplevel(self)
        dialog.title(title)
        dialog.transient(parent_window if parent_window else self)
        dialog.grab_set()
        dialog.resizable(False, False)
        
        # Set dialog size
        dialog_width = 350
        dialog_height = 150
        
        # Position relative to parent window
        if parent_window:
            parent_x = parent_window.winfo_x()
            parent_y = parent_window.winfo_y()
            parent_width = parent_window.winfo_width()
            parent_height = parent_window.winfo_height()
            
            # Position slightly left and down from parent center
            x = parent_x + (parent_width - dialog_width) // 2 - 50
            y = parent_y + (parent_height - dialog_height) // 2 + 30
        else:
            # Center on main window
            x = self.winfo_x() + (self.winfo_width() - dialog_width) // 2
            y = self.winfo_y() + (self.winfo_height() - dialog_height) // 2
            
        dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")
        
        # Create dialog content
        main_frame = ttk.Frame(dialog)
        main_frame.pack(fill="both", expand=True, padx=20, pady=20)
        
        # Message label
        message_label = ttk.Label(main_frame, text=message, wraplength=300, justify="center")
        message_label.pack(pady=(0, 20))
        
        # OK button
        button_frame = ttk.Frame(main_frame)
        button_frame.pack()
        
        def close_dialog():
            dialog.destroy()
            
        ok_button = ttk.Button(button_frame, text="OK", command=close_dialog)
        ok_button.pack()
        ok_button.focus_set()
        
        # Bind Enter key to close
        dialog.bind('<Return>', lambda e: close_dialog())
        dialog.bind('<Escape>', lambda e: close_dialog())
        
        # Wait for dialog to close
        dialog.wait_window()

    def _ask_export_format(self, default=export_formats.FORMAT_PDF):
        """Modal dialog med radioknapper. Returnerer format-nøglen eller None
        hvis brugeren annullerer (Esc / Annuller / luk-knap)."""
        dialog = tk.Toplevel(self)
        dialog.title(_("Vælg format"))
        dialog.transient(self)
        dialog.resizable(False, False)

        dialog_width, dialog_height = 320, 260
        x = self.winfo_x() + (self.winfo_width() - dialog_width) // 2
        y = self.winfo_y() + (self.winfo_height() - dialog_height) // 2
        dialog.geometry(f"{dialog_width}x{dialog_height}+{x}+{y}")

        result = {"fmt": None}
        fmt_var = tk.StringVar(value=default)

        main_frame = ttk.Frame(dialog)
        main_frame.pack(fill="both", expand=True, padx=20, pady=15)

        ttk.Label(main_frame, text=_("Vælg outputformat:"),
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 8))

        for key in export_formats.FORMAT_ORDER:
            ttk.Radiobutton(main_frame, text=export_formats.format_label(key),
                            variable=fmt_var, value=key).pack(anchor="w", pady=1)

        ttk.Label(main_frame,
                  text=_("Tekstformater bevarer ikke billeder og layout."),
                  wraplength=280, justify="left",
                  font=("Segoe UI", 8)).pack(anchor="w", pady=(10, 0))

        button_frame = ttk.Frame(main_frame)
        button_frame.pack(side="bottom", pady=(12, 0))

        def on_ok():
            result["fmt"] = fmt_var.get()
            dialog.destroy()

        def on_cancel():
            result["fmt"] = None
            dialog.destroy()

        ttk.Button(button_frame, text=_("OK"), command=on_ok).pack(side="left", padx=4)
        ttk.Button(button_frame, text=_("Annuller"), command=on_cancel).pack(side="left", padx=4)

        dialog.bind("<Return>", lambda _e: on_ok())
        dialog.bind("<Escape>", lambda _e: on_cancel())
        dialog.protocol("WM_DELETE_WINDOW", on_cancel)
        dialog.grab_set()
        dialog.wait_window()
        return result["fmt"]

    def _show_credits(self):
        """Vindue med licens, kildekodetilbud (AGPL §6) og bibliotekliste."""
        import webbrowser
        from . import credits

        win = tk.Toplevel(self)
        win.title(_("Credits"))
        win.transient(self)
        win.resizable(False, True)

        win_w, win_h = 520, 480
        x = self.winfo_x() + (self.winfo_width() - win_w) // 2
        y = self.winfo_y() + (self.winfo_height() - win_h) // 2
        win.geometry(f"{win_w}x{win_h}+{x}+{y}")

        outer = ttk.Frame(win, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Unite Docs " + __version__,
                  font=("Segoe UI", 12, "bold")).pack(anchor="w")
        ttk.Label(outer, text="© 2025 Bo Sundgaard",
                  font=("Segoe UI", 9)).pack(anchor="w", pady=(0, 8))

        ttk.Label(outer, text=_("Licens: AGPL-3.0"),
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")

        # AGPL §6 kildekodetilbud — det eneste ikke-valgfrie compliance-element.
        src_link = ttk.Label(outer, text=_("Vis kildekode"),
                             foreground=theme.C["link"], cursor="hand2",
                             font=("Segoe UI", 9, "underline"))
        src_link.bind("<Button-1>", lambda _e: webbrowser.open(credits.SOURCE_URL))
        src_link.pack(anchor="w", pady=(0, 12))

        ttk.Label(outer, text=_("Anvendte biblioteker"),
                  font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 4))

        # Rulbar liste.
        list_frame = ttk.Frame(outer)
        list_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(list_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(list_frame, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for lib in credits.CREDITS:
            row = ttk.Frame(inner)
            row.pack(fill="x", anchor="w", pady=2)
            ver = (" " + lib.version) if lib.version else ""
            ttk.Label(row, text=f"{lib.name}{ver}",
                      font=("Segoe UI", 9, "bold")).pack(anchor="w")
            ttk.Label(row, text=lib.license + "  —  " + lib.url,
                      font=("Segoe UI", 8), foreground=theme.C["text_muted"]).pack(anchor="w")

        ttk.Button(outer, text=_("OK"), command=win.destroy).pack(pady=(12, 0))

        win.bind("<Escape>", lambda _e: win.destroy())
        win.grab_set()

    def _update_export_status(self, p_widgets, extension):
        if not p_widgets['window'].winfo_exists():
            return
        p_widgets['label'].config(text=_("Konverterer til %s...") % extension)
        p_widgets['window'].update_idletasks()

    def _update_ocr_status(self, p_widgets):
        if not p_widgets['window'].winfo_exists():
            return
        p_widgets['label'].config(text=_("Gør scannede sider søgbare (OCR)..."))
        p_widgets['window'].update_idletasks()

    def _update_save_progress(self, p_widgets, current, total):
        if not p_widgets['window'].winfo_exists(): return
        p_widgets['bar']['value'] = (current / total) * 100
        p_widgets['label'].config(text=_("Behandler fil %s af %s...") % (current, total))
        p_widgets['window'].update_idletasks()

    def _update_export_progress(self, p_widgets, done, total):
        """Per-side fremdrift under den (langsomme) tekstkonvertering, med ETA."""
        if not p_widgets['window'].winfo_exists():
            return
        if p_widgets.get('_conv_start') is None:
            p_widgets['_conv_start'] = time.time()
        frac = (done / total) if total else 0
        p_widgets['bar']['value'] = frac * 100
        eta = ""
        if 0 < done < total:
            elapsed = time.time() - p_widgets['_conv_start']
            remaining = elapsed / done * (total - done)
            eta = " · " + (_("ca. %s tilbage") % self._fmt_secs(remaining))
        p_widgets['label'].config(
            text=(_("Konverterer side %(d)s af %(t)s") % {"d": done, "t": total}) + eta)
        p_widgets['window'].update_idletasks()

    def _destroy_progress(self, p_widgets):
        try:
            if p_widgets['window'].winfo_exists():
                p_widgets['window'].grab_release()
                p_widgets['window'].destroy()
        except tk.TclError:
            pass

    def _merge_cancelled(self, out_path, p_widgets):
        self._destroy_progress(p_widgets)
        try:
            if out_path and os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        messagebox.showinfo(_("Annulleret"), _("Eksporten blev annulleret."))

    def _save_dec_cancelled(self, p_widgets):
        self._destroy_progress(p_widgets)
        messagebox.showinfo(_("Annulleret"), _("Eksporten blev annulleret."))

    def _build_ui(self):
        # Kommandobar: én flad række med STORE ikoner over forklarende tekst
        # (compound="top"), adskilt af separatorer (ingen gruppe-overskrifter).
        # Primære handlinger er accent-blå. Indstillinger skubbes helt til højre
        # (Windows-konvention). En hårstreg under baren adskiller den fra indholdet.
        self.toolbar = ttk.Frame(self)
        self.toolbar.grid(row=0, column=0, sticky="ew")
        bar = ttk.Frame(self.toolbar)
        bar.pack(side="top", fill="x", padx=theme.SPACE["md"], pady=theme.SPACE["sm"])
        theme.hairline(self.toolbar, "horizontal").pack(side="top", fill="x")
        f = self.icon_factory
        CMD = theme.ICON["cmdlg"]          # store kommandobar-ikoner

        def sep():
            ttk.Separator(bar, orient="vertical").pack(
                side="left", fill="y", padx=theme.SPACE["sm"], pady=2)

        def primary(icon, text, command):
            b = ttk.Button(bar, text=text, style="Accent.TButton", compound="top",
                           image=f.get(icon, CMD, theme.C["selection_fg"]), command=command)
            b.pack(side="left", padx=1)
            return b

        def tool(icon, text, command, tip=None, shortcut=None):
            """Ikon-over-tekst knap. Synlig tekst er den forklarende label; en
            tooltip tilføjes kun når den bærer EKSTRA info (genvej/tilstand)."""
            b = ttk.Button(bar, text=text, style="Toolbutton", compound="top",
                           image=f.get(icon, CMD), command=command)
            b._icon_name = icon
            b._icon_size = CMD
            b.pack(side="left", padx=1)
            if tip is not None or shortcut is not None:
                Tooltip.attach(b, tip if tip is not None else text, shortcut=shortcut)
            return b

        # --- Filer ---
        primary('add', _("Tilføj filer"), self._add)
        self._merge_btn = primary('save', _("Flet og gem"), self._merge)
        tool('decrypt', _("Gem enkeltfiler"), self._save_dec)
        sep()

        # --- Roter / beskær ---
        tool('rotate_left', _("Venstre"), self._rotate_left)
        tool('rotate_right', _("Højre"), self._rotate_right)
        self._crop_btn = ttk.Button(bar, text=_("Beskær"), style="Toolbutton",
                                    compound="top", image=f.get('crop', CMD),
                                    command=self._toggle_crop_tool)
        self._crop_btn._icon_name = 'crop'
        self._crop_btn._icon_size = CMD
        self._crop_btn.pack(side="left", padx=1)
        Tooltip.attach(self._crop_btn, _("Træk en ramme for at beskære den viste side"))
        sep()

        # --- Rækkefølge: kompakt 2x2 pil-klynge (ikon-only m. tooltip) + omvend + slet ---
        order = ttk.Frame(bar)

        def ob(icon, tip, cmd, r, c, **grid):
            b = icons_vector.icon_button(order, icon=icon, tip=tip, command=cmd,
                                         factory=f, size=theme.ICON["small"],
                                         style="Compact.Toolbutton")
            b.grid(row=r, column=c, **grid)
        ob('up', _("Flyt op"), self._up, 0, 0, sticky="ew")
        ob('down', _("Flyt ned"), self._down, 1, 0, sticky="ew")
        ob('top', _("Flyt øverst"), self._move_top, 0, 1, sticky="ew", padx=(2, 0))
        ob('bottom', _("Flyt nederst"), self._move_bottom, 1, 1, sticky="ew", padx=(2, 0))
        order.pack(side="left", padx=(1, 4))
        self._sort_btn = ttk.Button(bar, text=_("Sorter"), style="Toolbutton",
                                    compound="top", image=f.get('sort', CMD),
                                    command=self._toggle_sort_panel)
        self._sort_btn._icon_name = 'sort'
        self._sort_btn._icon_size = CMD
        self._sort_btn.pack(side="left", padx=1)
        Tooltip.attach(self._sort_btn, _("Sortér filerne"))
        tool('insert_page', _("Indsæt side"), self._open_insert_page_dialog,
             tip=_("Indsæt en ny side med overskrift og tekst"))
        tool('delete', _("Slet"), self._delete_selection)
        sep()

        # --- Fortryd / gentag (ikon+tekst; ikon nedtones når disabled) ---
        self._undo_btn = ttk.Button(
            bar, style="Toolbutton", compound="top", state="disabled", command=self._do_undo,
            text=_("Fortryd"), image=f.get('undo_preview', CMD, theme.C["text_disabled"]))
        self._undo_btn._icon_name = 'undo_preview'
        self._undo_btn._icon_size = CMD
        Tooltip.attach(self._undo_btn, _("Fortryd"), shortcut="Ctrl+Z")
        self._undo_btn.pack(side="left", padx=1)
        self._redo_btn = ttk.Button(
            bar, style="Toolbutton", compound="top", state="disabled", command=self._do_redo,
            text=_("Gentag"), image=f.get('redo_preview', CMD, theme.C["text_disabled"]))
        self._redo_btn._icon_name = 'redo_preview'
        self._redo_btn._icon_size = CMD
        Tooltip.attach(self._redo_btn, _("Gentag"), shortcut="Ctrl+Y")
        self._redo_btn.pack(side="left", padx=1)
        sep()

        # --- Dialoger: kodeord (tooltip viser antallet) ---
        tool('key', _("Kodeord"), self._open_passwords_dialog,
             tip=lambda: _("Kodeord (%d gemt)") % len(self._pw_lines))

        # --- Højrestillet: Indstillinger yderst, visnings-skiftet lige til
        # venstre for den. Rækkefølgen er omvendt af det man ser: den FØRST
        # pakkede side="right"-widget havner længst til højre.
        settings_btn = ttk.Button(bar, text=_("Indstillinger"), style="Toolbutton",
                                  compound="top", image=f.get('settings', CMD),
                                  command=self._open_settings_window)
        settings_btn.pack(side="right")

        self.undo_stack.subscribe(self._update_undo_buttons)
        self._update_undo_buttons()

        # Sidegitteret er nu appens ENESTE dokumentvisning. Drop-highlight tegnes
        # af page_view selv (canvas-baggrunden), saa der er ingen ramme-widget her.
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.page_view = PageView(self, self)
        self.page_view.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)

        def _first_paint():
            self.page_view.rebuild()
            self.page_view.focus_page()      # tastaturnavigation klar med det samme

        self.after(50, _first_paint)

        # Bundpanelet (Adgangskoder) er flyttet til en dialog (nøgle-knappen i
        # kommandobaren), så dokumentet får hele højden. Row 2 står bevidst tom.

        self._build_status_bar()

    # --- Status bar (Fase 2) ----------------------------------------------
    def _build_status_bar(self):
        """Nederste linje: venstre = kontekstuel status, højre = version + Credits.
        Erstatter den gamle statiske versionsstribe."""
        foot = ttk.Frame(self)
        foot.grid(row=3, column=0, sticky="ew")
        theme.hairline(foot, "horizontal").pack(side="top", fill="x")
        sb = ttk.Frame(foot)
        sb.pack(side="top", fill="x", padx=theme.SPACE["md"], pady=(2, 4))
        sb.columnconfigure(0, weight=1)

        self._status_var = tk.StringVar(value=self._default_status())
        self._status_label = ttk.Label(sb, textvariable=self._status_var,
                                       foreground=theme.C["text_muted"], anchor="w")
        self._status_label.grid(row=0, column=0, sticky="w")

        right = ttk.Frame(sb)
        right.grid(row=0, column=1, sticky="e")
        ttk.Label(right, text="v%s · 2025 · Bo Sundgaard" % __version__,
                  foreground=theme.C["text_muted"], font=theme.FONTS["small"]).pack(side="left")

        def _dot():
            # Separator som egen dæmpet label -> understregningen dækker KUN
            # linkteksten, og de to links løber ikke sammen.
            ttk.Label(right, text="  ·  ", foreground=theme.C["text_muted"],
                      font=theme.FONTS["small"]).pack(side="left")

        def _link(text, command):
            lbl = ttk.Label(right, text=text, foreground=theme.C["link"],
                            cursor="hand2", font=theme.FONTS["link"])
            lbl.bind("<Button-1>", lambda _e: command())
            lbl.pack(side="left")

        _dot()
        _link("www.uniteapps.dk", self._open_website)
        _dot()
        _link(_("Credits"), self._show_credits)

    def _open_website(self):
        import webbrowser
        try:
            webbrowser.open("https://www.uniteapps.dk")
        except Exception as e:
            logger.warning("Kunne ikke åbne hjemmesiden: %s", e)

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
        try:
            sel = len(self.page_view.selected_uids()) if self.page_view else 0
        except (AttributeError, tk.TclError):
            sel = 0
        if sel:
            text += _("  ·  %(s)d valgt") % {"s": sel}
        return text

    def set_status(self, text: str = "", *, transient_ms: int | None = None, kind: str = "info"):
        """Skriv en statusbesked (main thread only — workere går via self._queue).

        Tom ``text`` viser tomgangs-teksten. ``transient_ms`` nulstiller til
        tomgang efter et stykke tid. ``kind`` styrer farven.
        """
        label = getattr(self, "_status_label", None)
        if label is None:
            return
        if self._status_after is not None:
            try:
                self.after_cancel(self._status_after)
            except (tk.TclError, ValueError):
                pass
            self._status_after = None
        colors = {"info": theme.C["text_muted"], "success": theme.C["success"],
                  "warning": theme.C["warning"], "error": theme.C["danger"]}
        msg = text if text else self._default_status()
        try:
            self._status_var.set(msg)
            label.configure(foreground=colors.get(kind, theme.C["text_muted"]))
        except tk.TclError:
            return
        if transient_ms:
            self._status_after = self.after(transient_ms, self._refresh_status)

    def _refresh_status(self):
        """Vis tomgangs-teksten igen (og annullér en evt. forbigående besked)."""
        self.set_status()

    def _process_queue(self):
        try:
            while True:
                fn, args = self._queue.get_nowait()
                try:
                    fn(*args)
                except Exception as e:
                    logger.error("Error processing queue item: %s", e)
                    import traceback
                    traceback.print_exc()
        except queue.Empty:
            pass
        self.after(50, self._process_queue)
    
    def _pwlist(self) -> list[str]:
        return list(dict.fromkeys(l.strip() for l in self._pw_lines if l.strip()))
    
    @property
    def paths(self) -> dict:
        """Read-only iid→path map derived from the model. Kept as a property so
        existing readers (password workers,
        merge) keep working while the model is the real source of truth. Writers
        must go through the model, not this dict.

        Read from worker threads (thumbnail existence checks), so iterate an atomic
        list() snapshot — a bare comprehension over self.model.files could raise
        "list changed size during iteration" while the main thread adds files."""
        return {f.iid: f.path for f in list(self.model.files)}

    def _add_file_entry(self, path, index="end"):
        """Læg en fil i modellen som PLADSHOLDER med det samme (ingen disk-I/O på
        hovedtråden). Returnerer den nye FileEntry, eller None hvis filen allerede
        er tilføjet. Metadata (sidetal, krypteringsstatus, dato, størrelse) læses
        asynkront bagefter via _load_metadata_async — det er dét der holder UI'et
        flydende når mange (eller krypterede) filer tilføjes på én gang."""
        if path in self.paths.values():
            return None
        entry = em.FileEntry(
            iid=uuid.uuid4().hex,
            path=path,
            kind=em.kind_for_path(path),
            enc_key=pdf_utils.ENC_UNKNOWN,       # placeholder until metadata loads
            creation_date="",
            source_page_count=0,
        )
        self.model.add_file(entry, None if index == "end" else index)
        return entry

    def _load_metadata_async(self, entries):
        """Read metadata for freshly-added files off the main thread and update
        their rows via the queue. One worker per batch; opens are serialized
        under PDF_LOCK (encrypted files try every password — that is exactly the
        slow work that used to freeze the UI)."""
        entries = [e for e in entries if e is not None]
        if not entries:
            return
        pw_list = self._get_all_passwords()
        snapshot = [(e.iid, e.path) for e in entries]
        threading.Thread(target=self._metadata_worker, args=(snapshot, pw_list),
                         daemon=True).start()

    def _metadata_worker(self, snapshot, pw_list):
        for iid, path in snapshot:
            with pdf_renderer.PDF_LOCK:
                meta = pdf_utils.get_pdf_metadata(path, pw_list)
            self._queue.put((self._apply_file_metadata, (iid, meta)))

    def _apply_file_metadata(self, iid, meta):
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return
        entry.enc_key = meta["enc_status"]
        entry.creation_date = meta["creation_date"]
        entry.size_bytes = int(meta.get("size_bytes") or 0)   # sorteringsnøgle
        if not entry.pages_loaded:
            entry.source_page_count = int(meta["page_count"]) if meta["page_count"] else 0
        if self.page_view is not None:
            # Hovedet viser navn/dato/hængelås — alle tre kan lige have ændret sig.
            self.page_view.refresh_header(iid)
            # Filen kan nu være læsbar og klar til at få sine sider populeret.
            self._schedule_page_view_refresh()
        # On-add prompt: en netop tilføjet, stadig-krypteret fil beder om kodeord.
        # Kun for filer der er markeret ved tilføjelse (ikke ved _refresh_all efter
        # gæt/tjek), så der ikke opstår en prompt-storm.
        if iid in self._prompt_on_metadata:
            if entry.enc_key == pdf_utils.ENC_ENCRYPTED:
                self._queue_unlock_prompt(iid)
            else:
                self._prompt_on_metadata.discard(iid)
        # Sidetal kan nu være kendt -> opdatér statuslinjen (respekterer en aktiv
        # forbigående besked ved ikke at overskrive den med det samme).
        if self._status_after is None:
            self._refresh_status()

    def _schedule_page_view_refresh(self):
        """Debounce many metadata callbacks into a single page-view rebuild."""
        if getattr(self, "_pv_refresh_after", None):
            return
        self._pv_refresh_after = self.after(150, self._do_page_view_refresh)

    def _do_page_view_refresh(self):
        self._pv_refresh_after = None
        if self.page_view is not None:
            self._ensure_pages_then_rebuild()

    def _update_undo_buttons(self):
        # ttk nedtoner ikke image= sammen med state="disabled" (billedet er ikke
        # en del af stilen), så ikonet males eksplicit i den rette tone.
        try:
            icons_vector.set_button_state(
                self._undo_btn, self.icon_factory,
                state="normal" if self.undo_stack.can_undo else "disabled", size=theme.ICON["cmd"])
            icons_vector.set_button_state(
                self._redo_btn, self.icon_factory,
                state="normal" if self.undo_stack.can_redo else "disabled", size=theme.ICON["cmd"])
        except (AttributeError, tk.TclError):
            pass

    def _do_undo(self, event=None):
        # Let Text widgets (kodeord / fritekst-felter) keep their own undo.
        if isinstance(self.focus_get(), tk.Text):
            return
        if not self.undo_stack.can_undo:
            return "break"
        self.undo_stack.undo()
        self._after_history_change()
        return "break"

    def _do_redo(self, event=None):
        if isinstance(self.focus_get(), tk.Text):
            return
        if not self.undo_stack.can_redo:
            return "break"
        self.undo_stack.redo()
        self._after_history_change()
        return "break"

    def _after_history_change(self):
        """Re-derive the views from the model after an undo/redo mutated it
        behind the UI's back."""
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()

    def after_model_change(self):
        """Genudled visningen efter en model-mutation. Eneste indgang for
        page_view, saa den ikke selv skal vide hvad der ellers lytter med."""
        self._refresh_status()

    def after_crop_change(self, uid=None):
        """En beskaering aendrede sidens maal: genopbyg gitteret, saa flisen
        gen-renderes (crop indgaar i render-cachens noegle)."""
        if self.page_view is not None:
            self.page_view.rebuild()
        self._refresh_status()

    def unlock_file(self, iid: str):
        """Bed om kodeord til een fil (fil-kontekstmenuen i sidevisningen)."""
        if self._prompt_password_for(iid):
            self._ensure_pages_then_rebuild()

    def delete_file(self, iid: str):
        """Slet en HEL fil (markeret filhoved i sidevisningen)."""
        if self.model.entry_by_iid(iid) is not None:
            self._remove([iid])

    def _count_redactions(self, entries):
        n = 0
        for f in entries:
            for pg in getattr(f, 'pages', []):
                for a in pg.annots:
                    if a.kind == annotations.ANNOT_REDACT:
                        n += 1
        return n

    def _confirm_redactions(self, entries):
        """Redaction is irreversible in the output (apply_redactions rewrites the
        content stream). Warn before any save that would bake redactions in."""
        n = self._count_redactions(entries)
        if n == 0:
            return True
        return messagebox.askyesno(
            _("Bekræft maskering"),
            _("Dokumentet indeholder %s maskering(er), som fjerner indhold "
              "permanent i den gemte fil og ikke kan fortrydes i outputtet. "
              "Vil du fortsætte?") % n,
            icon="warning")

    # --- Per-page export (Fase 10) ---------------------------------------
    def export_single_page(self, page_uid, fmt):
        """Export ONE page to its own file. fmt in {pdf, md, epub, jpg, png}.
        JPG/PNG are deliberately kept OUT of export_formats.FORMAT_ORDER (they are
        meaningless in the merge dialog); they render the finished page to a raster.
        """
        found = self.model.page_by_uid(page_uid)
        if not found:
            return
        entry, page = found
        # En indsat side har sin egen (ukrypterede) kildefil — låse-tjekket
        # gælder kun sider der faktisk kommer fra entry.path.
        own = os.path.normcase(page.src_path) == os.path.normcase(entry.path)
        if (own and entry.kind == em.KIND_PDF
                and entry.enc_key not in (pdf_utils.ENC_DECRYPTED, pdf_utils.ENC_NOT_ENCRYPTED)):
            messagebox.showinfo(_("Eksport"), _("Siden kan ikke eksporteres (låst fil)."))
            return
        ext = {"pdf": ".pdf", "md": ".md", "epub": ".epub",
               "jpg": ".jpg", "png": ".png"}.get(fmt)
        if not ext:
            return
        # Redaction is irreversible in the output — confirm if this page has any.
        if any(a.kind == annotations.ANNOT_REDACT for a in page.annots):
            if not self._confirm_redactions([entry]):
                return
        stem = Path(page.src_path).stem
        out_path = filedialog.asksaveasfilename(
            defaultextension=ext,
            initialfile="%s_side%d%s" % (stem, page.src_index + 1, ext),
            filetypes=[(fmt.upper(), "*" + ext)])
        if not out_path:
            return
        pdf_renderer.clear_doc_cache()
        pw = self._get_all_passwords()
        job = save_pipeline.FileJob(
            path=page.src_path,
            kind=entry.kind if own else em.kind_for_path(page.src_path),
            enc_key=entry.enc_key if own else pdf_utils.ENC_NOT_ENCRYPTED,
            pages=[save_pipeline.PageJob(src_index=page.src_index,
                                         rotation=page.rotation, annots=page.annots)],
            title=stem)
        p_widgets = self._show_progress_dialog(
            _("Eksporterer…"), _("Eksporterer side til %s…") % fmt.upper(),
            cancellable=True)
        self._export_busy = p_widgets
        threading.Thread(target=self._export_page_worker,
                         args=(out_path, job, pw, fmt, stem, p_widgets), daemon=True).start()

    def _export_page_worker(self, out_path, job, pw, fmt, stem, p_widgets):
        ev = p_widgets.get("cancel")
        def cancel():
            return ev is not None and ev.is_set()
        def export_progress(done, total):
            self._queue.put((self._update_export_progress, (p_widgets, done, total)))
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
                        pix = merged[0].get_pixmap(dpi=200)
                        pix.save(out_path)
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
                        fmt_const, doc=merged, out_path=out_path,
                        chapters=chapters, title=stem,
                        progress=export_progress, cancel=cancel)
                finally:
                    merged.close()
            self._queue.put((self._export_page_done, (out_path,)))
        except export_formats.ExportCancelled:
            self._queue.put((self._export_page_cancelled, (out_path,)))
        except Exception as e:
            logger.error("Per-side eksport fejlede: %s", e)
            self._queue.put((self._export_page_failed, (str(e),)))

    def _export_page_done(self, out_path):
        self._destroy_progress(getattr(self, "_export_busy", None) or {})
        self._export_busy = None
        messagebox.showinfo(_("Eksport"), _("Siden er eksporteret til:\n%s") % out_path)

    def _export_page_failed(self, msg):
        self._destroy_progress(getattr(self, "_export_busy", None) or {})
        self._export_busy = None
        messagebox.showerror(_("Eksport fejlede"), msg)

    def _export_page_cancelled(self, out_path):
        self._destroy_progress(getattr(self, "_export_busy", None) or {})
        self._export_busy = None
        try:
            if out_path and os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        messagebox.showinfo(_("Annulleret"), _("Eksporten blev annulleret."))

    def _add_paths(self, paths):
        added = [self._add_file_entry(p) for p in paths]
        self._mark_for_unlock_prompt(added)
        self._load_metadata_async(added)
        self._after_files_added()

    def _mark_for_unlock_prompt(self, entries):
        """Husk hvilke friskt tilføjede filer der skal prompte for kodeord, når
        deres metadata (og dermed enc-status) lander."""
        for e in entries:
            if e is not None:
                self._prompt_on_metadata.add(e.iid)

    def _add(self):
        filetypes = [(_("Understøttede filer"), "*.pdf *.jpg *.jpeg *.png *.bmp *.tiff *.tif"), (_("PDF-filer"), "*.pdf")]
        filetypes.append((_("Billedfiler"), "*.jpg *.jpeg *.png *.bmp *.tiff *.tif"))
        paths_to_add = filedialog.askopenfilenames(filetypes=filetypes)
        if not paths_to_add: return
        self._add_paths(paths_to_add)

    _SUPPORTED_DROP_EXTENSIONS = {'.pdf', '.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}

    def _on_external_drop(self, event):
        self._clear_drop_highlight()
        if not event.data:
            return event.action
        paths = self.tk.splitlist(event.data)
        # Insert at the drop position over the file tree (not always at the end).
        insert_index = self._drop_index_from_event(event)
        added = []
        for p in paths:
            if not os.path.exists(p):
                continue
            if Path(p).suffix.lower() not in self._SUPPORTED_DROP_EXTENSIONS:
                continue
            entry = self._add_file_entry(p, index=insert_index)
            if entry is not None:
                added.append(entry)
                if insert_index != "end":
                    insert_index += 1     # keep dropped files in order after target
        self._mark_for_unlock_prompt(added)
        self._load_metadata_async(added)
        self._after_files_added()
        return event.action

    def _drop_index_from_event(self, event):
        """Hvilket FIL-indeks et Explorer-drop skal indsaettes paa.

        Tidligere blev der hit-testet mod en traeraekke, og alt uden for en raekke
        gav "end" -- derfor landede ethvert drop i SIDEVISNINGEN altid nederst.
        Nu hit-testes der mod sidegitteret."""
        if self.page_view is None:
            return "end"
        try:
            idx = self.page_view.drop_file_index(event.x_root, event.y_root)
        except (AttributeError, tk.TclError):
            return "end"
        return "end" if idx is None else idx

    def _on_drop_enter(self, event):
        self._set_drop_highlight(True)
        self.set_status(_("Slip for at tilføje filer"))
        return event.action

    def _on_drop_leave(self, event):
        self._clear_drop_highlight()
        self._refresh_status()
        return event.action

    def _set_drop_highlight(self, on: bool):
        """Markér drop-maalet. Sidegitteret tegner selv sin baggrund om i
        _dnd_enter/_dnd_leave, saa her er der kun statuslinjen tilbage."""
        if on:
            self.set_status(_("Slip for at tilføje filerne"))
        else:
            self._refresh_status()

    def _clear_drop_highlight(self):
        self._set_drop_highlight(False)
        if self.page_view is not None:
            try:
                self.page_view.canvas.configure(background=theme.C["bg"])
            except tk.TclError:
                pass

    # --- View toggle (file view <-> page view, Fase 3) --------------------
    def _ensure_pages_then_rebuild(self):
        """Opret sider for enhver læsbar, endnu ikke populeret PDF (i en worker
        under PDF_LOCK) og genopbyg derefter gitteret. Billeder populeres med det
        samme (1 side, ingen I/O)."""
        for f in self.model.files:
            if f.kind == em.KIND_IMAGE and not f.pages_loaded:
                self.model.populate_image(f)
        to_load = [f for f in self.model.files
                   if f.kind == em.KIND_PDF and not f.pages_loaded
                   and f.enc_key in (pdf_utils.ENC_NOT_ENCRYPTED, pdf_utils.ENC_DECRYPTED)]
        if not to_load:
            self.page_view.rebuild()
            return
        pw_list = self._get_all_passwords()
        snapshot = [(f, f.path) for f in to_load]
        threading.Thread(target=self._populate_pages_worker,
                         args=(snapshot, pw_list), daemon=True).start()

    def _populate_pages_worker(self, snapshot, pw_list):
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

    def _populate_pages_done(self, counts):
        for f, n in counts:
            # Spring over hvis filen ER populeret: et sent worker-callback må
            # aldrig overskrive sidelisten, for så ville en undo af en flytning
            # eller udtrækning blive tromlet ned bagfra.
            if n > 0 and not f.pages_loaded:
                self.model.populate_pages(f, n)
        if self.page_view is not None:
            self.page_view.rebuild()

    def _after_files_added(self):
        """Populér og genopbyg gitteret efter at filer er tilføjet."""
        if self.page_view is not None:
            self._ensure_pages_then_rebuild()
        self._refresh_status()

    def _register_dnd(self, widget, on_enter=None, on_leave=None):
        """Register a widget as an Explorer drop target reusing the shared handlers.
        Used by the file tree and (Fase 3) the page view's tile canvas."""
        if not _tkdnd_support:
            return
        try:
            widget.drop_target_register(_DND_FILES)
            widget.dnd_bind('<<Drop>>', self._on_external_drop)
            widget.dnd_bind('<<DropEnter>>', on_enter or self._on_drop_enter)
            widget.dnd_bind('<<DropLeave>>', on_leave or self._on_drop_leave)
        except Exception as e:
            logger.warning("tkdnd registration failed: %s", e)

    def _remove(self, iids_to_remove: list[str] | None = None):
        """Fjern hele filer fra modellen (kildefilerne paa disken roeres ikke)."""
        if iids_to_remove is None:
            iids_to_remove = [self.page_view._selected_file] if (
                self.page_view and self.page_view._selected_file) else []
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

    def _delete_selection(self, event=None):
        """Kommandobarens Slet: sider hvis sider er markeret, ellers hele filen."""
        if self.page_view is None:
            return "break"
        sel = self.page_view.selected_uids()
        if sel:
            self.page_view.delete_pages(sel)
        elif self.page_view._selected_file:
            self._remove([self.page_view._selected_file])
        return "break"

    def _rotate_left(self):
        if self.page_view is not None:
            self.page_view.rotate_selected(-90)

    def _rotate_right(self):
        if self.page_view is not None:
            self.page_view.rotate_selected(90)
    
    def _nudge(self, direction: int):
        """Pil op/ned. Sider flyttes een plads; ved filens kant vandrer de over i
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
        elif self.page_view._selected_file:
            self.undo_stack.push(
                em.move_files_cmd(self.model, [self.page_view._selected_file], direction))
        else:
            return
        self.page_view.rebuild()
        self.page_view.reselect(sel)

    def _up(self):
        self._nudge(-1)

    def _down(self):
        self._nudge(1)

    def _move_edge(self, to_end: bool):
        """Flyt markeringen helt til dokumentets start/slut.

        Sider rives UD som deres egen fil forrest/bagerst -- ikke ind i den
        foerste/sidste eksisterende fil. "Flyt oeverst" paa en side midt i et
        dokument skal give en selvstaendig side foran alt andet; smed man den i
        stedet ind i nabofilen, blandede den sig med et helt andet dokument.
        En markeret FIL flytter derimod hele sin blok (med klaebende boern).
        """
        if self.page_view is None or not self.model.files:
            return
        sel = self.page_view.selected_uids()
        if sel:
            entry = self.model.page_by_uid(sel[0])[0]
            # Er filen allerede yderst og bestaar kun af markeringen, er der intet
            # at gore -- ellers ville vi slette og genskabe en identisk fil.
            edge = self.model.files[-1 if to_end else 0]
            if entry is edge and len(entry.pages) == len(sel):
                return
            self.undo_stack.push(em.extract_pages_cmd(
                self.model, sel, at_index=len(self.model.files) if to_end else 0))
        elif self.page_view._selected_file:
            iid = self.page_view._selected_file
            block = [iid] + [f.iid for f in self.model.files if f.origin_iid == iid]
            rest = [f.iid for f in self.model.files if f.iid not in set(block)]
            self.undo_stack.push(em.reorder_files_cmd(
                self.model, rest + block if to_end else block + rest))
        else:
            return
        self.page_view.rebuild()
        self.page_view.reselect(sel)

    def _move_top(self):
        self._move_edge(False)

    def _move_bottom(self):
        self._move_edge(True)

    def _refresh_all(self):
        """Genlaes metadata for hver fil off-thread (samme vej som ved tilfoejelse).
        Kaldes efter kodeordstjek/-gaet, hvor status kan have aendret sig."""
        snapshot = [(f.iid, f.path) for f in self.model.files]
        if not snapshot:
            return
        pw_list = self._get_all_passwords()
        threading.Thread(target=self._metadata_worker, args=(snapshot, pw_list),
                         daemon=True).start()

    # --- Sorter: et PERSISTENT panel, ikke en tk.Menu ---------------------
    # En tk.Menu lukker sig selv ved foerste klik. Panelet her er et
    # overrideredirect-Toplevel UDEN grab_set(), saa det bliver staaende indtil
    # man trykker Sorter igen (eller Escape).
    _SORT_ROWS = (
        (em.SORT_REVERSE, "Omvendt"),
        (em.SORT_DATE, "Oprettelsesdato"),
        (em.SORT_NAME, "Navn"),
        (em.SORT_SIZE, "Størrelse"),
    )

    def _toggle_sort_panel(self):
        if self._sort_panel is not None:
            self._close_sort_panel()
            return
        self._build_sort_panel()

    def _close_sort_panel(self, event=None):
        panel, self._sort_panel = self._sort_panel, None
        if panel is not None:
            try:
                panel.destroy()
            except tk.TclError:
                pass
        try:
            self.unbind("<Configure>", self._sort_cfg_bind)
        except (tk.TclError, AttributeError):
            pass
        self._sort_cfg_bind = None

    def _build_sort_panel(self):
        panel = tk.Toplevel(self)
        panel.overrideredirect(True)
        panel.transient(self)
        # Et overrideredirect-vindue haever sig ikke selv over hovedvinduet.
        panel.attributes("-topmost", True)
        self._sort_panel = panel
        frame = tk.Frame(panel, background=theme.C["surface"],
                         highlightthickness=1,
                         highlightbackground=theme.C["border_strong"],
                         highlightcolor=theme.C["border_strong"])
        frame.pack(fill="both", expand=True)
        f = self.icon_factory
        for key, label in self._SORT_ROWS:
            row = tk.Frame(frame, background=theme.C["surface"])
            row.pack(fill="x")
            arrow = ""
            if key != em.SORT_REVERSE:
                arrow = "▼" if self._sort_dir.get(key) else "▲"
            lbl = tk.Label(row, text=_(label), anchor="w", padx=10, pady=5,
                           background=theme.C["surface"], foreground=theme.C["text"],
                           font=theme.FONTS["base"])
            lbl.pack(side="left", fill="x", expand=True)
            dirlbl = tk.Label(row, text=arrow, padx=8,
                              background=theme.C["surface"],
                              foreground=theme.C["text_muted"],
                              font=theme.FONTS["small"])
            dirlbl.pack(side="right")
            for w in (row, lbl, dirlbl):
                w.bind("<Button-1>", lambda e, k=key: self._apply_sort(k))
                w.bind("<Enter>", lambda e, r=row, l=lbl, d=dirlbl:
                       [x.configure(background=theme.C["hover"]) for x in (r, l, d)])
                w.bind("<Leave>", lambda e, r=row, l=lbl, d=dirlbl:
                       [x.configure(background=theme.C["surface"]) for x in (r, l, d)])
        self._position_sort_panel()
        panel.bind("<Escape>", self._close_sort_panel)
        # Panelet skal FOELGE vinduet, ikke blive hængende et tilfældigt sted.
        self._sort_cfg_bind = self.bind("<Configure>",
                                        lambda e: self._position_sort_panel(), add="+")

    def _position_sort_panel(self):
        panel = self._sort_panel
        if panel is None:
            return
        try:
            btn = self._sort_btn
            panel.update_idletasks()
            x = btn.winfo_rootx()
            y = btn.winfo_rooty() + btn.winfo_height() + 2
            panel.geometry("+%d+%d" % (x, y))
        except tk.TclError:
            pass

    def _apply_sort(self, key: str):
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
            self._close_sort_panel()
            self._build_sort_panel()

    def _toggle_crop_tool(self):
        """Slaa beskaerings-vaerktoejet til i fremviseren."""
        if self.page_view is None:
            return
        self.page_view.activate_crop_tool()

    def _save_dec(self):
        # Bed om kodeord til låste filer først, så de kan komme med i gemningen.
        self._ensure_all_unlocked()
        pdf_iids = [f.iid for f in self.model.files
                    if f.enc_key in (pdf_utils.ENC_DECRYPTED, pdf_utils.ENC_NOT_ENCRYPTED)]
        if not pdf_iids:
            messagebox.showinfo(_("Gem"), _("Ingen PDF-filer at gemme."))
            return

        # Release any page-view-held source handles so a save can't self-lock them.
        pdf_renderer.clear_doc_cache()
        fmt = self._ask_export_format()
        if not fmt:
            return

        dest_folder = filedialog.askdirectory(title=_("Vælg mappe til at gemme filer"))
        if not dest_folder:
            return

        total = len(pdf_iids)
        # Byg Tk-frie jobs fra de valgte (dekrypterbare) model-entries på hovedtråden.
        entries = [e for e in (self.model.entry_by_iid(i) for i in pdf_iids) if e is not None]
        if not self._confirm_redactions(entries):
            return
        jobs = save_pipeline.build_jobs(entries)

        # Tjek at ingen af de tilsigtede målfiler er låst af et andet program FØR
        # arbejdet starter. En fil der blot findes (men ikke er låst) håndteres
        # stadig af _unique_save_path med et _1-suffiks — kun en fil der er åben i
        # et andet program stopper os, så vi ikke tavst laver en _1-kopi i stedet.
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
            target = Path(dest_folder) / name
            if self._is_path_locked(str(target)):
                locked.append(target.name)
        if locked:
            return messagebox.showerror(
                _("Filen er i brug"),
                _("Følgende fil(er) er åben i et andet program:\n\n%s\n\n"
                  "Luk dem og prøv igen.") % "\n".join(locked))
        p_widgets = self._show_progress_dialog(_("Gemmer filer"), _("Forbereder at gemme %s filer...") % total, cancellable=True)
        self.set_status(_("Gemmer…"))
        threading.Thread(target=self._save_dec_worker,
                         args=(jobs, self._get_all_passwords(), p_widgets, dest_folder, fmt),
                         daemon=True).start()

    def _save_dec_worker(self, jobs, pw_list, p_widgets, dest_folder, fmt):
        ev = p_widgets.get("cancel")
        def cancel():
            return ev is not None and ev.is_set()
        def progress_report(i, total):
            self._queue.put((self._update_save_progress, (p_widgets, i, total)))
        def ocr_report():
            self._queue.put((self._update_ocr_status, (p_widgets,)))
        def export_report(ext):
            self._queue.put((self._update_export_status, (p_widgets, ext)))
        def export_progress(done, total):
            self._queue.put((self._update_export_progress, (p_widgets, done, total)))
        try:
            res = save_pipeline.save_each_worker(
                jobs, pw_list, fmt, dest_folder,
                apply_annots=annotations.apply_specs_to_page,
                progress_report=progress_report, ocr_report=ocr_report,
                export_report=export_report, export_progress=export_progress, cancel=cancel)
        except export_formats.ExportCancelled:
            self._queue.put((self._save_dec_cancelled, (p_widgets,)))
            return
        self._queue.put((self._save_dec_complete,
                         (res["saved"], res["failed"], res["missing_pages"], p_widgets, dest_folder)))

    def _save_dec_complete(self, saved_count, failed_count, missing_pages,
                           p_widgets, dest_folder=None):
        p_widgets['window'].destroy()
        if failed_count > 0:
            self.set_status(_("%(ok)d gemt · %(fail)d fejlede")
                            % {"ok": saved_count, "fail": failed_count},
                            transient_ms=8000, kind="warning")
            messagebox.showwarning(
                _("Gem"),
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
                self._show_saved_dialog(_("Gem"), msg, Path(dest_folder),
                                        is_folder=True)
            else:
                messagebox.showinfo(_("Gem"), msg)
    
    def _is_path_locked(self, path):
        """True hvis filen findes men ikke kan åbnes til skrivning — typisk fordi
        den er åben i et andet program (fx en PDF-læser der låser filen).

        Testen er den samme kapabilitet ``save`` skal bruge: kan vi åbne målet
        til skrivning uden at ændre indholdet. En fil der ikke findes endnu er
        aldrig låst."""
        if not os.path.exists(path):
            return False
        try:
            with open(path, "r+b"):
                pass
            return False
        except OSError:
            return True

    def _merge(self):
        if not self.model.files:
            return messagebox.showwarning(_("Ingen filer"), _("Tilføj filer først"))
        # On-merge: bed om kodeord til hver stadig-låst fil før fletning.
        self._ensure_all_unlocked()
        if not self._confirm_redactions(self.model.files):
            return
        # Release any page-view-held source handles so a save can't self-lock them.
        pdf_renderer.clear_doc_cache()
        fmt = self._ask_export_format()
        if not fmt:
            return
        spec = export_formats.EXPORT_FORMATS[fmt]
        out_path = filedialog.asksaveasfilename(
            defaultextension=spec.extension,
            initialfile="Flettet" + spec.extension,
            filetypes=export_formats.format_filetypes(fmt))
        if not out_path: return
        # Tjek at målfilen ikke er låst FØR fletningen starter — ellers opdages
        # det først ved save, efter alt arbejdet er gjort.
        if self._is_path_locked(out_path):
            return messagebox.showerror(
                _("Filen er i brug"),
                _("Filen \"%s\" er åben i et andet program.\n"
                  "Luk den og prøv igen.") % out_path)
        # Snapshot alt Tk-afhængigt på hovedtråden — workeren må ikke røre widgets.
        # Byg en Tk-fri job-liste fra modellen (i listens rækkefølge). Selve
        # to-pas-fletningen ligger i save_pipeline (headless-testbar).
        jobs = save_pipeline.build_jobs(self.model.files)
        total = len(jobs)
        p_widgets = self._show_progress_dialog(_("Fletter PDF'er"), _("Forbereder at flette %s filer...") % total, cancellable=True)
        self.set_status(_("Fletter…"))
        threading.Thread(target=self._merge_worker,
                         args=(out_path, self._get_all_passwords(), p_widgets, fmt, jobs),
                         daemon=True).start()

    def _merge_worker(self, out_path, pw_list, p_widgets, fmt, jobs):
        ev = p_widgets.get("cancel")
        def cancel():
            return ev is not None and ev.is_set()
        def report(i, total):
            self._queue.put((self._update_save_progress, (p_widgets, i, total)))
        def ocr_report():
            self._queue.put((self._update_ocr_status, (p_widgets,)))
        def export_report(ext):
            self._queue.put((self._update_export_status, (p_widgets, ext)))
        def export_progress(done, total):
            self._queue.put((self._update_export_progress, (p_widgets, done, total)))
        try:
            res = save_pipeline.merge_worker(
                out_path, jobs, pw_list, fmt,
                apply_annots=annotations.apply_specs_to_page,
                report=report, ocr_report=ocr_report, export_report=export_report,
                export_progress=export_progress, cancel=cancel)
            self._queue.put((self._merge_complete,
                             (res["ok"], res["fail"], res["missing_pages"], out_path, p_widgets)))
        except export_formats.ExportCancelled:
            self._queue.put((self._merge_cancelled, (out_path, p_widgets)))
        except export_formats.ExportError as e:
            self._queue.put((self._merge_error, (e, p_widgets)))
        except Exception as e:
            self._queue.put((self._merge_error, (e, p_widgets)))

    def _merge_complete(self, ok, fail, missing_pages, out_path, p_widgets):
        p_widgets['window'].destroy()
        self.set_status(_("Gemt: %(name)s") % {"name": Path(out_path).name},
                        transient_ms=8000, kind="success")
        msg = _("%(ok)s filer behandlet korrekt\n%(fail)s filer fejlede") \
            % {"ok": ok, "fail": fail}
        if missing_pages > 0:
            msg += "\n" + _("%s side(r) uden tekstlag blev ikke udtrukket") % missing_pages
        msg += "\n\n" + _("Filen er gemt som:")
        self._show_saved_dialog(_("Fletning fuldført"), msg, Path(out_path),
                                is_folder=False)

    def _merge_error(self, error, p_widgets):
        p_widgets['window'].destroy()
        self.set_status(_("Fletning fejlede"), transient_ms=8000, kind="error")
        messagebox.showerror(_("Fejl"), _("Kunne ikke gemme filen:\n%s") % error)

    def _build_guess_progress_window(self, title, items):
        """Byg det fælles fremdriftsvindue for kodeords-tjek/gæt.

        De to kaldere (_start_check_only og _run_guessing_process) havde tidligere
        identisk vindue-opbygning. Returnerer (win, stop_btn, on_close); kalderen
        sætter selv self._stop_guessing/self._stop_event før kaldet og starter
        worker-tråden bagefter.
        """
        win = tk.Toplevel(self)
        win.title(title)
        # 720x520 (fra 500x500): giver plads til filnavn + 200px progressbar +
        # status uden at status-kolonnen klippes.
        win.geometry("720x520")

        def on_close():
            self._stop_guessing = True
            self._stop_event.set()
            self._refresh_all()
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)
        cont = ttk.Frame(win)
        cont.pack(fill="both", expand=True, padx=8, pady=8)
        canvas = tk.Canvas(cont)
        vsb = ttk.Scrollbar(cont, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frm = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        # Lad det indre frame følge canvas-bredden, så kolonne 0 (filnavn) kan give
        # plads frem for at presse status-kolonnen ud af syne.
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win_id, width=e.width))
        frm.columnconfigure(0, weight=1)
        frm.columnconfigure(2, minsize=180)

        self._guess_widgets = {}
        for r, iid in enumerate(items):
            ttk.Label(frm, text=Path(self.paths[iid]).name).grid(row=r, column=0, sticky="w")
            pb = ttk.Progressbar(frm, length=200, mode="indeterminate")
            pb.grid(row=r, column=1, padx=6)
            lbl = ttk.Label(frm, text=_("Vent..."))
            lbl.grid(row=r, column=2, sticky="w")
            self._guess_widgets[iid] = (pb, lbl)
        stop_btn = ttk.Button(win, text=_("Stop"), command=lambda: (setattr(self, '_stop_guessing', True), self._stop_event.set()))
        stop_btn.pack(pady=4)
        return win, stop_btn, on_close

    def _start_check_only(self, items=None):
        if items is None:
            items = [f.iid for f in self.model.files
                     if f.enc_key == pdf_utils.ENC_ENCRYPTED]
        if not items:
            return messagebox.showinfo(_("Tjek kodeliste"), _("Der er ingen krypterede PDF-filer på listen."))
        self._stop_guessing = False
        self._stop_event = threading.Event()
        win, stop_btn, on_close = self._build_guess_progress_window(_("Tjekker kodeord fra liste"), items)

        def finalize():
            self._refresh_all()
            try:
                if stop_btn.winfo_exists():
                    stop_btn.config(text=_("Luk"), command=on_close)
            except tk.TclError:
                pass
        # Snapshot known passwords on the main thread; the worker must not read widgets.
        known = self._get_all_passwords()
        threading.Thread(target=lambda: (self._check_passwords_only_worker(items, known), self._queue.put((finalize, ()))), daemon=True).start()

    def _check_passwords_only_worker(self, items, known_passwords):
        # Local copy so newly found passwords can be tried against later files
        # without re-reading widgets from this worker thread.
        known = list(known_passwords)
        for iid in items:
            if self._stop_guessing: break
            self._queue.put((self._start_guess_pb, (iid,)))

            found_pw: str | None = None
            for pw in known:
                if self._stop_guessing:
                    break
                if pdf_utils.open_with_passwords(self.paths[iid], [pw]):
                    found_pw = pw
                    break

            if found_pw:
                if found_pw not in known:
                    known.append(found_pw)
                # Dedup happens on the main thread inside _append_password.
                self._queue.put((self._append_password, (found_pw,)))
                self._save_password_to_cache(found_pw)
                self._queue.put((self._update_status, (iid, _("Fundet: %s") % found_pw)))
                # Use thumbnail queue instead of individual threads (Fix for Bug 1)
                self.page_render_mgr.invalidate_path(self.paths[iid])
            else:
                self._queue.put((self._update_status, (iid, _("Ikke fundet"))))

            self._queue.put((self._stop_guess_pb, (iid,)))
            if not self._stop_guessing:
                time.sleep(0.05)

    def _start_guessing(self, items=None):
        # Create a dialog to choose mode
        dialog = tk.Toplevel(self)
        dialog.title(_("Vælg metode"))
        dialog.geometry("350x250")
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        
        # Center dialog
        x = self.winfo_x() + (self.winfo_width() - 350) // 2
        y = self.winfo_y() + (self.winfo_height() - 250) // 2
        dialog.geometry(f"+{x}+{y}")
        
        ttk.Label(dialog, text=_("Vælg type af gætning:"), font=("Segoe UI", 10, "bold")).pack(pady=(15, 10))
        
        # Modes
        mode_numeric_var = tk.BooleanVar(value=True)

        # Numeric option
        f1 = ttk.Frame(dialog)
        f1.pack(fill="x", padx=30, pady=5)
        c1 = ttk.Checkbutton(f1, text=_("Numerisk (korte koder)"), variable=mode_numeric_var)
        c1.pack(side="left")

        # Length config
        len_frame = ttk.Frame(dialog)
        len_frame.pack(fill="x", padx=50)
        ttk.Label(len_frame, text=_("Antal cifre:")).pack(side="left")
        # Default from config (fallback 5)
        current_max = self.config.getint("Security", "bruteforce_max_len", fallback=5)
        len_var = tk.IntVar(value=current_max)
        ttk.Spinbox(len_frame, from_=1, to=10, textvariable=len_var, width=5).pack(side="left", padx=5)

        # Buttons
        btn_frame = ttk.Frame(dialog)
        btn_frame.pack(side="bottom", pady=20)

        def start_action():
            modes = {
                'numeric': mode_numeric_var.get(),
            }
            if not any(modes.values()):
                messagebox.showwarning(_("Fejl"), _("Vælg mindst én metode."), parent=dialog)
                return

            max_len = len_var.get()
            dialog.destroy()
            self._run_guessing_process(modes, max_len, items=items)

        ttk.Button(btn_frame, text=_("Start"), command=start_action).pack(side="left", padx=5)
        ttk.Button(btn_frame, text=_("Annuller"), command=dialog.destroy).pack(side="left", padx=5)

    def _run_guessing_process(self, modes, max_len, items=None):
        if items is None:
            items = [f.iid for f in self.model.files
                     if f.enc_key == pdf_utils.ENC_ENCRYPTED]
        if not items:
            return messagebox.showinfo(_("Gæt kodeord"), _("Der er ingen krypterede PDF-filer på listen."))
            
        self._stop_guessing = False
        self._stop_event = threading.Event()
        win, stop_btn, on_close = self._build_guess_progress_window(_("Gæt / Tjek kodeord"), items)

        def finalize():
            self._refresh_all()
            try:
                if stop_btn.winfo_exists():
                    stop_btn.config(text=_("Luk"), command=on_close)
            except tk.TclError:
                pass
                
        # Snapshot known passwords on the main thread; the worker must not read widgets.
        known = self._get_all_passwords()
        # Pass parameters to worker
        threading.Thread(target=lambda: (self._guess_passwords(items, modes, max_len, known), self._queue.put((finalize, ()))), daemon=True).start()
    
    def _update_status(self, iid, txt):
        try:
            _, lbl = self._guess_widgets.get(iid, (None, None))
            if lbl and lbl.winfo_exists():
                lbl['text'] = txt
        except tk.TclError:
            pass

    # --- Main-thread helpers for the password workers (never call from a worker
    #     thread directly; queue them so all widget access happens on the UI thread). ---
    def _start_guess_pb(self, iid):
        pb = self._guess_widgets.get(iid, (None, None))[0]
        if pb and pb.winfo_exists():
            pb.start()

    def _stop_guess_pb(self, iid):
        pb = self._guess_widgets.get(iid, (None, None))[0]
        if pb and pb.winfo_exists():
            pb.stop()

    def _append_password(self, p):
        """Tilføj et kodeord til app-tilstanden (main thread). Opdaterer den åbne
        kodeord-dialog hvis den er fremme."""
        p = (p or "").strip()
        if not p or p in self._pw_lines:
            return
        self._pw_lines.append(p)
        self._pw_cache = None
        self._refresh_pw_dialog()

    # --- Password manager dialog (erstatter det gamle bundpanel) -----------
    def _open_passwords_dialog(self):
        """Dialog til at se/redigere kodeordslisten manuelt + Tjek/Gæt over alle
        krypterede filer. (Prompten ved åbning/tilføjelse er den primære vej;
        denne dialog er til manuel styring.)"""
        if self._pw_dialog is not None and self._pw_dialog.winfo_exists():
            self._pw_dialog.deiconify()
            self._pw_dialog.lift()
            return
        dlg = tk.Toplevel(self)
        dlg.title(_("Adgangskoder"))
        dlg.transient(self)
        dlg.resizable(True, True)
        self._pw_dialog = dlg

        ttk.Label(dlg, text=_("Bruges til at åbne krypterede PDF'er. Ét kodeord pr. linje."),
                  padding=(10, 10, 10, 4)).pack(anchor="w")

        body = ttk.Frame(dlg, padding=(10, 0, 10, 0))
        body.pack(fill="both", expand=True)
        txt = tk.Text(body, width=40, height=10, wrap="none")
        theme.style_text(txt)
        txt.insert("1.0", "\n".join(self._pw_lines))
        sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        self._pw_dialog_text = txt

        btns = ttk.Frame(dlg, padding=10)
        btns.pack(fill="x")

        def commit():
            self._commit_pw_dialog(txt)

        def close():
            commit()
            self._pw_dialog = None
            self._pw_dialog_text = None
            dlg.destroy()

        ttk.Button(btns, text=_("Tjek kodeliste"),
                   command=lambda: (commit(), self._start_check_only())).pack(side="left")
        ttk.Button(btns, text=_("Gæt kodeord"),
                   command=lambda: (commit(), self._start_guessing())).pack(side="left", padx=(6, 0))
        ttk.Button(btns, text=_("OK"), style="Accent.TButton",
                   command=close).pack(side="right")
        dlg.protocol("WM_DELETE_WINDOW", close)
        dlg.bind("<Escape>", lambda e: close())

        # Placér relativt til hovedvinduet.
        dlg.update_idletasks()
        x = self.winfo_x() + 60
        y = self.winfo_y() + 80
        dlg.geometry("+%d+%d" % (x, y))
        txt.focus_set()

    def _commit_pw_dialog(self, txt):
        try:
            lines = [l.strip() for l in txt.get("1.0", "end").splitlines() if l.strip()]
        except tk.TclError:
            return
        self._pw_lines = list(dict.fromkeys(lines))
        self._pw_cache = None

    def _refresh_pw_dialog(self):
        """Gen-synk dialogens tekstfelt fra self._pw_lines (fx når et gættet
        kodeord tilføjes). No-op hvis dialogen er lukket."""
        dlg = self._pw_dialog
        txt = getattr(self, "_pw_dialog_text", None)
        if dlg is None or txt is None:
            return
        try:
            if not dlg.winfo_exists() or not txt.winfo_exists():
                return
            txt.delete("1.0", "end")
            txt.insert("1.0", "\n".join(self._pw_lines))
        except tk.TclError:
            pass

    # --- Unlock (prompt for password on add / open / merge) ----------------
    def _mark_unlocked(self, iid: str, pw: str | None):
        """Markér en fil som dekrypteret, gem kodeordet og genopfrisk miniature."""
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return
        entry.enc_key = pdf_utils.ENC_DECRYPTED
        if pw:
            self._append_password(pw)
            self._save_password_to_cache(pw)
        self._prompt_on_metadata.discard(iid)
        # Miniaturen viste hængelåsen; gen-render nu den er læsbar.
        self.page_render_mgr.invalidate_path(entry.path)
        if self.page_view is not None:
            self.page_view.refresh_header(iid)
        if self.page_view is not None:
            self._schedule_page_view_refresh()

    def _try_known_unlock(self, iid: str) -> bool:
        """Prøv de kendte kodeord (GUI + cache) mod filen. True hvis den åbner
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
        """Modal: bed om kodeord til én krypteret fil. Prøver de kendte kodeord
        først. Tilbyder at gætte. Returnerer True hvis filen blev låst op."""
        entry = self.model.entry_by_iid(iid)
        if entry is None:
            return False
        if entry.enc_key != pdf_utils.ENC_ENCRYPTED:
            return True
        if self._try_known_unlock(iid):
            return True

        name = Path(entry.path).name
        dlg = tk.Toplevel(self)
        dlg.title(_("Kodeord påkrævet"))
        dlg.transient(self)
        dlg.resizable(False, False)
        result = {"ok": False}

        ttk.Label(dlg, text=_("Filen \"%s\" er beskyttet med kodeord.") % name,
                  wraplength=360, padding=(12, 12, 12, 6)).pack(anchor="w")
        row = ttk.Frame(dlg, padding=(12, 0, 12, 0))
        row.pack(fill="x")
        pwvar = tk.StringVar()
        ent = ttk.Entry(row, textvariable=pwvar, show="●", width=34)
        ent.pack(side="left", fill="x", expand=True)
        showvar = tk.BooleanVar(value=False)

        def toggle_show():
            ent.configure(show="" if showvar.get() else "●")
        ttk.Checkbutton(dlg, text=_("Vis kodeord"), variable=showvar,
                        command=toggle_show, padding=(12, 2, 12, 2)).pack(anchor="w")
        err = ttk.Label(dlg, text="", foreground=theme.C["danger"], padding=(12, 0, 12, 0))
        err.pack(anchor="w")

        def submit(_e=None):
            pw = pwvar.get()
            if not pw:
                return
            doc = pdf_utils.open_with_passwords(entry.path, [pw])
            if doc is not None:
                doc.close()
                self._mark_unlocked(iid, pw)
                result["ok"] = True
                dlg.destroy()
            else:
                err.configure(text=_("Forkert kodeord. Prøv igen."))
                pwvar.set("")
                ent.focus_set()

        def do_guess():
            # Brugeren kender ikke kodeordet -> gæt. Afbryd resten af køen, så
            # der ikke stables prompts oven på gætte-dialogen.
            self._pending_unlock.clear()
            dlg.destroy()
            self._guess_single_file(iid)

        def cancel(_e=None):
            dlg.destroy()

        btns = ttk.Frame(dlg, padding=12)
        btns.pack(fill="x")
        ttk.Button(btns, text=_("Lås op"), style="Accent.TButton",
                   command=submit).pack(side="right")
        ttk.Button(btns, text=_("Gæt kodeord"),
                   command=do_guess).pack(side="right", padx=(0, 6))
        ttk.Button(btns, text=_("Spring over"),
                   command=cancel).pack(side="left")

        ent.bind("<Return>", submit)
        dlg.bind("<Escape>", cancel)
        dlg.update_idletasks()
        dlg.geometry("+%d+%d" % (self.winfo_x() + 80, self.winfo_y() + 120))
        ent.focus_set()
        try:
            dlg.grab_set()
        except tk.TclError:
            pass
        self.wait_window(dlg)
        return result["ok"]

    def _guess_single_file(self, iid: str):
        """Start gætte-flowet for netop én fil (genbruger metode-/fremdriftsdialogerne)."""
        if self.model.entry_by_iid(iid) is None:
            return
        self._start_guessing(items=[iid])

    def _queue_unlock_prompt(self, iid: str):
        """Sæt en fil i kø til en modal kodeord-prompt (bruges ved tilføjelse)."""
        if iid not in self._pending_unlock:
            self._pending_unlock.append(iid)
        self.after_idle(self._drain_unlock_prompts)

    def _drain_unlock_prompts(self):
        """Kør de køede prompts én ad gangen (aldrig genindtrædende)."""
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

    def _ensure_all_unlocked(self):
        """Prompt for hver stadig-låst krypteret fil før flet/gem/split. Fortsætter
        uanset (pipelinen rapporterer selv filer der stadig er låste)."""
        for iid in [f.iid for f in self.model.files]:
            entry = self.model.entry_by_iid(iid)
            if entry is not None and entry.enc_key == pdf_utils.ENC_ENCRYPTED:
                if not self._try_known_unlock(iid):
                    self._prompt_password_for(iid)

    def _guess_passwords(self, items, modes, max_len=5, known_passwords=None):
        # Local copy so newly found passwords can be tried against later files
        # without re-reading widgets from this worker thread.
        known = list(known_passwords or [])
        for iid in items:
            if self._stop_guessing: break
            self._queue.put((self._start_guess_pb, (iid,)))

            found_pw: str | None = None

            # First check cache/known passwords (stop-aware)
            for pw in known:
                if self._stop_guessing:
                    break
                if pdf_utils.open_with_passwords(self.paths[iid], [pw]):
                    found_pw = pw
                    break

            # Check Numeric if selected
            if not found_pw and not self._stop_guessing and modes.get('numeric'):
                msg = _("Numerisk (max %s)...") % max_len
                self._queue.put((self._update_status, (iid, msg)))

                found_pw = password_guesser.guess_password(
                    self.paths[iid],
                    max_len=max_len,
                    ui_stop_flag=self._stop_event
                )

            if found_pw:
                if found_pw not in known:
                    known.append(found_pw)
                # Dedup happens on the main thread inside _append_password.
                self._queue.put((self._append_password, (found_pw,)))
                self._save_password_to_cache(found_pw)
                self._queue.put((self._update_status, (iid, _("Fundet: %s") % found_pw)))
                # Use thumbnail queue instead of individual threads (Fix for Bug 1)
                self.page_render_mgr.invalidate_path(self.paths[iid])
            else:
                self._queue.put((self._update_status, (iid, _("Ikke fundet"))))

            self._queue.put((self._stop_guess_pb, (iid,)))
            if not self._stop_guessing:
                time.sleep(0.05)

    def _open_settings_window(self):
        settings_win = tk.Toplevel(self)
        settings_win.title(_("Indstillinger"))
        settings_win.transient(self)
        settings_win.grab_set()
        settings_win.resizable(False, False)
        settings_win.configure(background=theme.C["bg"])
        settings_win.language_changed = False

        # --- Layout: én ensartet to-kolonne-grid. Label-kolonnen har fast bredde,
        #     så alle kontroller flugter lodret på tværs af sektioner (som i
        #     Windows-/Adobe-indstillinger). Sektionsoverskrift = fed label + hårstreg.
        content = ttk.Frame(settings_win, padding=(18, 14, 18, 8))
        content.pack(fill="both", expand=True)
        content.columnconfigure(0, minsize=210, weight=0)
        content.columnconfigure(1, weight=1)
        row = [0]

        def section(title):
            if row[0] > 0:
                ttk.Frame(content, height=10).grid(row=row[0], column=0, columnspan=2)
                row[0] += 1
            ttk.Label(content, text=title, font=theme.FONTS["strong"]).grid(
                row=row[0], column=0, columnspan=2, sticky="w", pady=(2, 3))
            row[0] += 1
            ttk.Separator(content, orient="horizontal").grid(
                row=row[0], column=0, columnspan=2, sticky="ew", pady=(0, 8))
            row[0] += 1

        def field(label, widget):
            ttk.Label(content, text=label).grid(
                row=row[0], column=0, sticky="w", padx=(0, 14), pady=5)
            widget.grid(row=row[0], column=1, sticky="w", pady=5)
            row[0] += 1

        def full(widget, pady=5):
            widget.grid(row=row[0], column=0, columnspan=2, sticky="w", pady=pady)
            row[0] += 1

        def hint(text):
            ttk.Label(content, text=text, foreground=theme.C["text_muted"],
                      font=theme.FONTS["small"], wraplength=420, justify="left").grid(
                row=row[0], column=0, columnspan=2, sticky="w", pady=(0, 2))
            row[0] += 1

        # --- Kodeordsgætning ---
        section(_("Kodeordsgætning"))
        current_max_len = self.config.getint("Security", "bruteforce_max_len", fallback=5)
        self.max_len_var = tk.IntVar(value=current_max_len)
        field(_("Maksimal længde at gætte:"),
              ttk.Spinbox(content, from_=1, to=10, textvariable=self.max_len_var, width=6))
        hint(_("Antal cifre i numeriske koder brute-force forsøger."))

        # --- Sprog ---
        section(_("Sprog"))
        language_names = LocalizationManager.get_supported_languages()
        self.language_var = tk.StringVar(
            value=LocalizationManager.get_language_name(self.language_code))
        lang_combobox = ttk.Combobox(content, textvariable=self.language_var,
                                     values=language_names, state="readonly", width=24)
        field(_("Sprog:"), lang_combobox)
        hint(_("Sprogændringer træder i kraft, når du lukker vinduet."))

        def on_language_selected(_e=None):
            lang_code = LocalizationManager.get_language_code(self.language_var.get())
            if lang_code == self.language_code:
                settings_win.language_changed = False
                return
            self.config.set("General", "language", lang_code)
            self.config.save()
            settings_win.language_changed = True
        lang_combobox.bind("<<ComboboxSelected>>", on_language_selected)

        # --- Windows-integration ---
        section(_("Windows-integration"))
        ctx_var = tk.BooleanVar(value=context_menu.is_registered())

        def _toggle_ctx():
            try:
                if ctx_var.get():
                    context_menu.register()
                else:
                    context_menu.unregister()
            except Exception as e:
                self._show_custom_dialog(_("Fejl"), str(e), "error", parent_window=settings_win)
                ctx_var.set(not ctx_var.get())
        full(ttk.Checkbutton(
            content,
            text=_("Tilføj 'Flet med UniteDocs' til højreklik-menuen for PDF-filer"),
            variable=ctx_var, command=_toggle_ctx))

        # --- Opdateringer ---
        section(_("Opdateringer"))
        auto_var = tk.BooleanVar(
            value=self.config.getboolean("Updates", "auto_check", fallback=True))

        def _toggle_auto_update():
            # Skrives med det samme (som sprogvalget), ikke ved lukning — så
            # indstillingen overlever også en hård afslutning.
            self.config.set("Updates", "auto_check", "1" if auto_var.get() else "0")
            self.config.save()

        full(ttk.Checkbutton(
            content,
            text=_("Søg automatisk efter opdateringer (én gang om ugen)"),
            variable=auto_var, command=_toggle_auto_update))
        hint(_("Unite Docs kontakter www.uniteapps.dk og spørger altid, "
               "før noget hentes eller installeres."))
        field(_("Manuel kontrol:"),
              ttk.Button(content, text=_("Søg efter opdateringer"),
                         command=lambda: self._start_update_check(manual=True)))

        skipped_version = (self.config.get("Updates", "skipped_version",
                                           fallback="") or "").strip()
        if skipped_version:
            # Uden denne udvej er "spring denne version over" en enkeltrettet dør.
            reset_link = ttk.Label(
                content,
                text=_("Nulstil oversprunget version (%s)") % skipped_version,
                foreground=theme.C["link"], cursor="hand2", font=theme.FONTS["link"])

            def _reset_skipped(_e=None):
                self.config.set("Updates", "skipped_version", "")
                self.config.save()
                reset_link.configure(text=_("Oversprunget version er nulstillet."),
                                     foreground=theme.C["text_muted"],
                                     font=theme.FONTS["small"], cursor="")
                reset_link.unbind("<Button-1>")
            reset_link.bind("<Button-1>", _reset_skipped)
            full(reset_link, pady=(0, 4))

        # --- Vedligeholdelse ---
        section(_("Vedligeholdelse"))
        field(_("Gemte kodeord:"),
              ttk.Button(content, text=_("Slet gemte kodeord"),
                         command=self._clear_password_cache))

        # --- Log ---
        section(_("Log"))
        hint(_("Logfilen kan hjælpe med fejlfinding."))

        def _make_link(parent, text, command):
            link = ttk.Label(parent, text=text, foreground=theme.C["link"],
                             cursor="hand2", font=theme.FONTS["link"])
            link.bind("<Button-1>", lambda _e: command())
            return link
        log_links = ttk.Frame(content)
        _make_link(log_links, _("Download log"),
                   lambda: self._download_log(settings_win)).pack(side="left")
        ttk.Label(log_links, text="   ").pack(side="left")
        _make_link(log_links, _("Send log via e-mail"),
                   lambda: self._email_log(settings_win)).pack(side="left")
        full(log_links)

        # --- Bundlinje: én afsluttende knap (apply-on-close) ---
        ttk.Separator(settings_win, orient="horizontal").pack(fill="x")
        bar = ttk.Frame(settings_win, padding=(18, 10))
        bar.pack(fill="x")

        def save_max_len_silent():
            try:
                v = int(self.max_len_var.get())
            except (ValueError, tk.TclError):
                return
            v = max(1, min(10, v))
            self.max_len_var.set(v)
            self.config.set("Security", "bruteforce_max_len", str(v))
            self.config.save()

        def on_settings_close():
            save_max_len_silent()
            if settings_win.language_changed:
                new_lang_code = self.config.get("General", "language", fallback="en")
                self.language_code = new_lang_code
                global _
                LocalizationManager.get_instance().set_language(new_lang_code)
                _ = LocalizationManager.get_text
                # PreviewWindow uses LocalizationManager.get_text internally.
                self._rebuild_ui_for_language_change()
            settings_win.destroy()

        ttk.Button(bar, text=_("Luk"), style="Accent.TButton",
                   command=on_settings_close).pack(side="right")
        settings_win.bind("<Escape>", lambda e: on_settings_close())

        # Placér øverst-til-højre for hovedvinduet og størrelse efter indhold.
        settings_win.update_idletasks()
        w = settings_win.winfo_reqwidth()
        x = self.winfo_x() + self.winfo_width() - w - 50
        y = self.winfo_y() + 50
        settings_win.geometry("+%d+%d" % (max(0, x), max(0, y)))

        settings_win.protocol("WM_DELETE_WINDOW", on_settings_close)

    def _download_log(self, parent_window=None):
        """Gem en kopi af logfilen et sted brugeren vælger."""
        import shutil
        from .logging_config import get_log_path

        log_path = get_log_path()
        if not log_path.exists():
            self._show_custom_dialog(
                _("Log"), _("Der er ingen logfil endnu."), "info", parent_window=parent_window
            )
            return

        dest = filedialog.asksaveasfilename(
            title=_("Gem logfil"),
            defaultextension=".log",
            initialfile="unitedocs.log",
            filetypes=[(_("Logfiler"), "*.log"), (_("Alle filer"), "*.*")],
        )
        if not dest:
            return
        try:
            shutil.copy2(log_path, dest)
        except Exception as e:
            logger.error("Kunne ikke gemme logfil: %s", e)
            self._show_custom_dialog(
                _("Fejl"), _("Kunne ikke gemme logfilen: %s") % e, "error", parent_window=parent_window
            )

    def _email_log(self, parent_window=None):
        """Åbn brugerens mailklient forudfyldt med de nyeste log-linjer.

        Vedhæftning er ikke mulig via ``mailto``, og Windows afkorter lange
        mailto-links; derfor indlejres kun de sidste linjer af loggen — nok til
        at se den seneste fejl uden at sprænge mailto-længdegrænsen.
        """
        import webbrowser
        from urllib.parse import quote
        from .logging_config import get_log_path

        log_path = get_log_path()

        # De sidste ~1000 tegn (afrundet til hele linjer) er nok til at vise
        # den seneste fejl og holder mailto-linket inden for Windows'
        # længdegrænse (~2048 tegn efter URL-encoding).
        log_tail = _("(ingen logfil endnu)")
        try:
            if log_path.exists():
                text = log_path.read_text(encoding="utf-8", errors="replace").rstrip()
                tail = text[-1000:]
                if len(text) > 1000:
                    # Undgå at starte midt i en linje.
                    tail = tail[tail.find("\n") + 1:]
                if tail.strip():
                    log_tail = tail
        except Exception as e:
            logger.warning("Kunne ikke læse logfil til e-mail: %s", e)

        subject = _("UniteDocs log - version %s") % __version__
        body = (
            _("Beskriv venligst problemet her:")
            + "\n\n\n"
            + _("--- Seneste log ---")
            + "\n"
            + log_tail
        )
        mailto = "mailto:kontakt@uniteapps.dk?subject=%s&body=%s" % (quote(subject), quote(body))
        try:
            webbrowser.open(mailto)
        except Exception as e:
            logger.error("Kunne ikke åbne mailklient: %s", e)
            self._show_custom_dialog(
                _("Fejl"), _("Kunne ikke åbne e-mailprogrammet: %s") % e, "error", parent_window=parent_window
            )

    def _rebuild_ui_for_language_change(self):
        """Rydder og genopbygger brugerfladen for at afspejle sprogændringer."""
        # Ingen planlagt tooltip må overleve widget-nedrivningen: en after()-
        # callback der vækker en destrueret widget giver TclError.
        Tooltip.hide_all()
        # Annullér en planlagt status-nulstilling før statuslinjen destrueres.
        if self._status_after is not None:
            try:
                self.after_cancel(self._status_after)
            except (tk.TclError, ValueError):
                pass
            self._status_after = None
        # Samme for opstartens opdateringstjek: er det stadig planlagt, ville
        # det vågne op i et halvt genopbygget vindue.
        if self._update_after_id is not None:
            try:
                self.after_cancel(self._update_after_id)
            except (tk.TclError, ValueError):
                pass
            self._update_after_id = None

        # Åbne dialoger kan ikke overleve at hele træet rives ned.
        self._pw_dialog = None
        self._insert_dialog = None
        # Kodeordslisten lever i app-tilstand (self._pw_lines, ikke widgets) og
        # overlever genopbygningen automatisk.
        # EditModel BEHOLDES — det ejer iid'erne, sidetal, rotationer og annotationer.

        # Nulstil UI-referencer først (før widgets ødelægges)
        self._close_sort_panel()

        # Ryd eksisterende widgets
        for widget in self.winfo_children():
            widget.destroy()

        # Kun widget-bundne caches ryddes; modellen, rotationer og croppings består
        # (rotations/croppings er keyet på model-ejede iid'er der overlever).

        # Genindlæs UI-komponenter (kodeordstilstanden er allerede bevaret).
        self._load_icons()
        self._build_ui()

        # Sidegitteret genudledes fra modellen med SAMME iid'er (ingen disk-læsning).
        if self.page_view is not None:
            self.page_view.rebuild()

    def _clear_password_cache(self):
        cache_path = self._get_password_cache_path()
        if cache_path.exists():
            try:
                os.remove(cache_path)
                self._pw_cache = None   # invalidér den kombinerede in-memory liste
                messagebox.showinfo(_("Cache slettet"), _("Alle gemte kodeord er slettet."))
            except Exception as e:
                messagebox.showerror(_("Fejl"), _("Kunne ikke slette cache: %s") % e)
        else:
            messagebox.showinfo(_("Cache"), _("Ingen kodeordscache fundet."))