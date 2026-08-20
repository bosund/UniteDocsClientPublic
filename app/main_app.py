import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from pathlib import Path
import os
import sys
import queue
import threading
import uuid
import datetime
import time
from PIL import Image, ImageTk
import pikepdf
from pikepdf import Pdf
from . import pdf_renderer  # New renderer using pypdfium2
from .preview_window import PreviewWindow
from . import pdf_utils
from . import utils
from . import password_guesser
from .config import AppConfig
from . import __version__
from .localization import LocalizationManager
from . import context_menu
from .logging_config import get_logger

logger = get_logger(__name__)
import gettext

# INTERNATIONALIZATION: Global translation function
# AI ASSISTANTS: Always use _("text") for user-visible strings
# Example: ttk.Label(parent, text=_("Save file"))
# After adding new _("strings"), run: python update_locales.py
_ = LocalizationManager.get_text

try:
    from ttkthemes import ThemedTk
    _ttkthemes_support = True
except ImportError:
    _ttkthemes_support = False

# Temaer der fjernes fra den kompilerede version (se _prune_ttkthemes i
# compiler/kompilerv3.py). Kun 'arc' og 'scid*' pakkes med; et config-tema
# fra denne liste ville crashe ved opstart, saa der faldes tilbage til 'arc'.
_PRUNED_THEMES = {
    "adapta", "aquativo", "black", "blue", "breeze", "clearlooks",
    "elegance", "equilux", "itft1", "keramik", "keramik_alt", "kroc",
    "plastik", "radiance", "smog", "ubuntu", "winxpblue", "yaru",
}

try:
    import tkinterdnd2 as _tkdnd_pkg
    from tkinterdnd2 import DND_FILES as _DND_FILES
    _tkdnd_support = True
except ImportError:
    _tkdnd_support = False

from .thumbnail_manager import ThumbnailManager

class PDFTool(ThemedTk if _ttkthemes_support else tk.Tk):
    def __init__(self, initial_files: list[str] | None = None):
        self.config = AppConfig()
        self.language_code = self.config.get("General", "language", fallback="en")
        
        # Initialize Localization Manager (Fix for Bug 1)
        LocalizationManager.initialize(self.language_code)
        
        if _ttkthemes_support:
            theme = self.config.get("General", "theme", fallback="arc")
            if theme in _PRUNED_THEMES:
                theme = "arc"
            super().__init__(theme=theme)
        else:
            super().__init__()

        if _tkdnd_support:
            try:
                _tkdnd_pkg.TkinterDnD._require(self)
            except Exception as e:
                logger.warning("tkdnd init failed: %s", e)

        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self.bind_all("<Escape>", self._cancel_drag)
        try:
            self.iconbitmap(utils.resource_path("icon.ico"))
        except tk.TclError:
            logger.info("'icon.ico' not found. Using default icon.")

        self.title(f"Unite Docs – v{__version__}")
        self.geometry("985x780")
        self.minsize(985, 480)

        # Internal state containers
        self.paths = {}
        self.thumbnail_cache = {}
        # self._encrypted_placeholder_cache managed by ThumbnailManager now, but kept if needed or removed?
        # Removing self._encrypted_placeholder_cache from here as it is not used in main_app anymore except by the moved methods.
        self.rotations = {}
        self.croppings = {}

        # Performance: in-memory cache for the combined GUI + disk password list.
        # Invalidated by _save_password_to_cache() whenever a new password is persisted.
        self._pw_cache: list[str] | None = None

        # Initialize Thumbnail Manager
        self.thumbnail_manager = ThumbnailManager(self)

        self._stop_guessing = False
        self._sort_reverse = {"Filnavn": False, "Sider": False, "Krypteret": False, "CreationDate": False}
        self.open_previews = {}
        self.add_headline_var = tk.BooleanVar(value=False)
        self._drag_data = {}
        self._drag_window = None
        self._drop_indicator = None

        # Build UI + async queue loop
        self._load_icons()
        self._build_ui()
        self._queue = queue.Queue()
        self.after(50, self._process_queue)
        
        # Start thumbnail worker via manager
        self.thumbnail_manager.start_worker()
        
        # Start periodic cleanup via manager
        # Start periodic cleanup via manager
        self.after(30000, self.thumbnail_manager.periodic_cleanup)

        # --- Dynamic Resizing (Language Support) ---
        # Adjust window width if translated buttons require more space
        self.update_idletasks()
        req_width = self.toolbar.winfo_reqwidth() + 60  # Add padding for safety
        if req_width > 985:
            self.geometry(f"{req_width}x780")
            self.minsize(req_width, 480)

        if initial_files:
            self.after(300, lambda: self._add_paths(initial_files))

        self.after(200, self._start_ipc)

    def _load_icons(self):
        self.icons = {}
        icon_definitions = {
            'add': ('add-files.png', (24, 24)),
            'split': ('page-split.png', (24, 24)),
            'update': ('update.png', (24, 24)),
            'rotate_left': ('rotate-left.png', (24, 24)),
            'rotate_right': ('rotate-right.png', (24, 24)),
            'save': ('save.png', (24, 24)),
            'decrypt': ('decrypt.png', (24, 24)),
            'check': ('check-passwords.png', (24, 24)),
            'guess': ('guess-passwords.png', (24, 24)),
            'swap': ('move-swap.png', (24, 24)),
            'top': ('move-top.png', (15, 15)),
            'bottom': ('move-bottom.png', (15, 15)),
            'up': ('move-up.png', (15, 15)),
            'down': ('move-down.png', (15, 15)),
            'delete': ('page-delete.png', (24, 24)),
            'undo_preview': ('undo_25.png', (24, 24)),
            'ok': ('ok.png', (24, 24)),
            'cancel_preview': ('annuller_25.png', (24, 24)),
            'zoom_in': ('zoom-in.png', (24, 24)),
            'zoom_out': ('zoom-out.png', (24, 24)),
            'crop': ('crop.png', (24, 24)),
            'remove_crop_preview': ('crop-reset.png', (24, 24)),
            'settings': ('settings.png', (24, 24)), # Placeholder for settings icon
            'lock': ('lock.png', (24, 24)),  # Bruges til krypteret placeholder
        }
        for name, (filename, size) in icon_definitions.items():
            try:
                icon_path = utils.resource_path(f"icons/{filename}")
                icon_img = Image.open(icon_path).resize(size, Image.Resampling.LANCZOS)
                self.icons[name] = ImageTk.PhotoImage(icon_img)
            except (FileNotFoundError, OSError) as e:
                messagebox.showerror(
                    _("Ikon Fejl"),
                    (_("Kunne ikke indlæse ikonet: %s") % filename) + "\n\n"
                    + _("Kontroller at filen eksisterer i 'icons'-mappen og er en gyldig billedfil.") + "\n\n"
                    + (_("Systemfejl: %s") % e)
                )
                break
            except Exception as e:
                logger.error("Unexpected error loading icon %s: %s", filename, e)
                break

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

    def _on_closing(self):
        try:
            from . import ipc
            ipc.cleanup_ipc()
        except Exception:
            pass
        try:
            if hasattr(self, 'thumbnail_manager'):
                 self.thumbnail_manager.stop_worker()
                 self.thumbnail_manager.cleanup_temp_files()
        finally:
            self.destroy()

    # _cleanup_temp_files moved to ThumbnailManager
    
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

    def _enc_status_label(self, key: str) -> str:
        """Translate a stable pdf_utils.ENC_* key to a localized display label.
        Only for display in the tree — never use the label for logic comparisons."""
        labels = {
            pdf_utils.ENC_ENCRYPTED: _("Krypteret"),
            pdf_utils.ENC_DECRYPTED: _("Dekrypteret"),
            pdf_utils.ENC_NOT_ENCRYPTED: _("Ikke krypteret"),
            pdf_utils.ENC_IMAGE: _("Billede/Andet"),
            pdf_utils.ENC_ERROR: _("Fejl/Ukendt"),
            pdf_utils.ENC_UNKNOWN: _("Ukendt"),
        }
        return labels.get(key, key)

    def _toggle_headline_entry(self):
        if self.add_headline_var.get():
            self.headline_entry.config(state="normal")
        else:
            self.headline_entry.config(state="disabled")

    def _create_title_page_pdf(self, title_text: str):
        try:
            import io
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import A4
            from reportlab.platypus import Paragraph
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.lib.enums import TA_CENTER

            pdf_buffer = io.BytesIO()
            c = canvas.Canvas(pdf_buffer, pagesize=A4)
            width, height = A4
            margin = 40 
            available_width = width - 2 * margin
            formatted_text = title_text.replace('\n', '<br/>')
            style = ParagraphStyle(
                name='TitlePageStyle',
                fontName='Helvetica-Bold',
                fontSize=28,
                leading=28 * 1.5,
                alignment=TA_CENTER
            )
            p = Paragraph(formatted_text, style)
            p_width, p_height = p.wrapOn(c, available_width, height)
            p.drawOn(c, margin, (height - p_height) / 2)
            c.showPage()
            c.save()
            pdf_buffer.seek(0)
            return pdf_buffer
        except (ImportError, Exception) as e:
            logger.error("Fejl ved oprettelse af forside: %s", e)
            return None
    
    def _show_progress_dialog(self, title: str, text: str) -> dict:
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self)
        win.grab_set()
        win.geometry("400x120")
        win.resizable(False, False)
        ttk.Label(win, text=text, font=("Segoe UI", 10)).pack(pady=(10,0), padx=10)
        status_label = ttk.Label(win, text=_("Initialiserer..."), font=("Segoe UI", 9))
        status_label.pack(pady=5, padx=10, anchor="w")
        bar = ttk.Progressbar(win, mode='determinate', length=350)
        bar.pack(expand=True, fill="x", padx=20, pady=(0, 15))
        win.update()
        return {"window": win, "bar": bar, "label": status_label}

    def report_callback_exception(self, exc, val, tb):
        """Log undtagelser fra Tk-callbacks (knapper, after(), events).

        Tk sluger normalt disse og skriver dem kun til stderr, som er tom i en
        vinduesbygget exe — så de forsvandt fra loggen. Nu logges de.
        """
        logger.critical("Ufanget undtagelse i Tk-callback", exc_info=(exc, val, tb))

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

    def _update_save_progress(self, p_widgets, current, total):
        if not p_widgets['window'].winfo_exists(): return
        p_widgets['bar']['value'] = (current / total) * 100
        p_widgets['label'].config(text=_("Behandler fil %s af %s...") % (current, total))
        p_widgets['window'].update_idletasks()

    def _get_original_pil_image(self, iid: str) -> Image.Image | None:
        """Get the original uncropped, unrotated image for preview window"""
        file_path = self.paths.get(iid)
        if not file_path:
            return None
        try:
            passwords = self._get_all_passwords()
            img = pdf_renderer.render_first_page(file_path, passwords, dpi=150)
            return img
        except Exception as e:
            logger.debug("Kunne ikke lave fuldt billede for %s: %s", Path(file_path).name, e)
            return None

    def _get_full_pil_image(self, iid: str) -> Image.Image | None:
        file_path = self.paths.get(iid)
        if not file_path:
            return None
        try:
            passwords = self._get_all_passwords()
            img = pdf_renderer.render_first_page(file_path, passwords, dpi=150)
            if img:
                # Apply rotation/crop if user changed preview edits
                rotation = self.rotations.get(iid, 0)
                crop_box = self.croppings.get(iid)
                if crop_box:
                    img = img.crop(crop_box)
                if rotation != 0:
                    img = img.rotate(-rotation, expand=True)
            return img
        except Exception as e:
            logger.debug("Kunne ikke lave fuldt billede for %s: %s", Path(file_path).name, e)
            return None
        finally:
            if 'img' not in locals() or img is None:
                logger.debug("Render mislykkedes for %s. Ikke nødvendigvis krypteret.", file_path)

    # _create_thumbnail and _get_encrypted_placeholder moved to ThumbnailManager
                
    def _build_ui(self):
        self.toolbar = ttk.Frame(self)
        toolbar = self.toolbar
        toolbar.grid(row=0, column=0, sticky="ew", padx=8, pady=6)
        files_group = ttk.Frame(toolbar)
        ttk.Label(files_group, text=_("Filfunktioner")).pack(anchor="center")
        files_buttons = ttk.Frame(files_group)
        ttk.Button(files_buttons, text=_("Tilføj filer"), image=self.icons.get('add'), compound="top", command=self._add).pack(side="left")
        ttk.Button(files_buttons, text=_("Split sider"), image=self.icons.get('split'), compound="top", command=self._split_pdf).pack(side="left", padx=(2,0))
        ttk.Button(files_buttons, text=_("Flet og gem"), image=self.icons.get('save'), compound="top", command=self._merge).pack(side="left", padx=(2,0))
        ttk.Button(files_buttons, text=_("Gem enkeltfiler"), image=self.icons.get('decrypt'), compound="top", command=self._save_dec).pack(side="left", padx=(2,0))
        files_buttons.pack()
        files_group.pack(side="left")
        
        ttk.Label(toolbar, text="|", foreground="gray").pack(side="left", padx=10)
        
        rotate_group = ttk.Frame(toolbar)
        ttk.Label(rotate_group, text=_("Roter fil")).pack(anchor="center")
        rotate_buttons = ttk.Frame(rotate_group)
        ttk.Button(rotate_buttons, text=_("Venstre"), image=self.icons.get('rotate_left'), compound="top", command=self._rotate_left).pack(side="left")
        ttk.Button(rotate_buttons, text=_("Højre"), image=self.icons.get('rotate_right'), compound="top", command=self._rotate_right).pack(side="left", padx=(2,0))
        rotate_buttons.pack()
        rotate_group.pack(side="left")
        
        ttk.Label(toolbar, text="|", foreground="gray").pack(side="left", padx=10)
        
        order_group = ttk.Frame(toolbar)
        ttk.Label(order_group, text=_("Filrækkefølge")).pack(anchor="center")
        order_buttons = ttk.Frame(order_group)
                                
        ttk.Button(order_buttons, image=self.icons.get('up'), command=self._up).grid(row=0, column=0, sticky="ew")
        ttk.Button(order_buttons, image=self.icons.get('down'), command=self._down).grid(row=1, column=0, sticky="ew")
        
        ttk.Button(order_buttons, image=self.icons.get('top'), command=self._move_top).grid(row=0, column=1, padx=(2,0), sticky="ew")
        ttk.Button(order_buttons, image=self.icons.get('bottom'), command=self._move_bottom).grid(row=1, column=1, padx=(2,0), sticky="ew")
        
        ttk.Button(order_buttons, text=_("Omvendt"), image=self.icons.get('swap'), compound="top", command=self._reverse).grid(row=0, column=2, rowspan=2, padx=(6, 0), sticky="ns")
        
        ttk.Button(order_buttons, text=_("Slet side"), image=self.icons.get('delete'), compound="top", command=self._remove).grid(row=0, column=3, rowspan=2, padx=(6, 0), sticky="ns")
        order_buttons.pack()
        order_group.pack(side="left")

        ttk.Label(toolbar, text="|", foreground="gray").pack(side="left", padx=10)

        settings_group = ttk.Frame(toolbar)
        ttk.Label(settings_group, text=_("Indstillinger")).pack(anchor="center")
        settings_buttons = ttk.Frame(settings_group)
        ttk.Button(settings_buttons, text=_("Indstillinger"), image=self.icons.get('settings'), compound="top", command=self._open_settings_window).pack(side="left")
        settings_buttons.pack()
        settings_group.pack(side="left")

        frame = ttk.Frame(self)
        frame.grid(row=1, column=0, sticky="nsew", padx=8, pady=6)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(0, weight=1)
        # "EncKey" is a hidden column carrying the stable internal encryption-status
        # key (pdf_utils.ENC_*). The visible "Krypteret" column shows the localized
        # label. Logic must read "EncKey", never the localized display value.
        self.tree = ttk.Treeview(frame, columns=("Filnavn", "Sider", "Krypteret", "CreationDate", "EncKey"), displaycolumns=("Filnavn", "Sider", "Krypteret", "CreationDate"), show="tree headings", selectmode="extended", height=15)
        self.tree.column("#0", width=65, anchor="center", stretch=False)
        self.tree.heading("#0", text=_("Mini"))
        self.tree.heading("Filnavn", text=_("Filnavn"), command=lambda: self._sort_column("Filnavn"))
        self.tree.heading("Sider", text=_("Sider"), command=lambda: self._sort_column("Sider"))
        self.tree.heading("Krypteret", text=_("Status"), command=lambda: self._sort_column("Krypteret"))
        self.tree.heading("CreationDate", text=_("Oprettelsesdato"), command=lambda: self._sort_column("CreationDate"))
        self.tree.column("Filnavn", width=400)
        self.tree.column("Sider", width=50, anchor="center")
        self.tree.column("Krypteret", width=100, anchor="center")
        self.tree.column("CreationDate", width=140, anchor="center")
        
        style = ttk.Style()
        style.configure("Treeview", rowheight=60)
        style.configure("DropTarget.Treeview", rowheight=60, relief="solid", borderwidth=2)
        style.map("DropTarget.Treeview", background=[("", "#E8F4FF")])
        self.tree.tag_configure('drag_selection', background='#0078D7', foreground='white')
        
        # --- Event Bindings ---
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Button-3>", self._show_context_menu)
        self.tree.bind("<Button-2>", self._show_context_menu)
        self.tree.bind("<Shift-KeyPress-Up>", self._on_shift_arrow_select)
        self.tree.bind("<Shift-KeyPress-Down>", self._on_shift_arrow_select)
        self.tree.bind("<ButtonPress-1>", self._on_drag_start)
        self.tree.bind("<B1-Motion>", self._on_drag_motion)
        self.tree.bind("<ButtonRelease-1>", self._on_drag_drop)
        self.tree.bind("<Delete>", self._remove_selected_from_event)

        if _tkdnd_support:
            try:
                self.tree.drop_target_register(_DND_FILES)
                self.tree.dnd_bind('<<Drop>>', self._on_external_drop)
                self.tree.dnd_bind('<<DropEnter>>', self._on_drop_enter)
                self.tree.dnd_bind('<<DropLeave>>', self._on_drop_leave)
            except Exception as e:
                logger.warning("tkdnd registration failed: %s", e)

        vsb = ttk.Scrollbar(frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, columnspan=2, sticky="ew")
        
        self._drop_indicator = tk.Frame(self.tree, bg="#0078D7", height=2)
        self._create_context_menu()

        pwf = ttk.Frame(self)
        pwf.grid(row=2, column=0, sticky="ew", padx=8, pady=6)
        pwf.columnconfigure(1, weight=1)
        
        pw_frame = ttk.LabelFrame(pwf, text=_("Adgangskoder"), padding=6)
        pw_frame.grid(row=0, column=0, sticky="ns", pady=(0, 6))
        pw_frame.columnconfigure(0, weight=1)
        ttk.Label(pw_frame, text=_("Bruges til at åbne krypterede PDF'er.")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 2))
        pw_text_frame = ttk.Frame(pw_frame)
        self.pw = tk.Text(pw_text_frame, height=6, width=35)
        self.pw.bind("<<Modified>>", self._on_pw_text_modified)
        self.pw.pack(side="left", fill="both", expand=True)
        pw_scrollbar = ttk.Scrollbar(pw_text_frame, orient="vertical", command=self.pw.yview)
        pw_scrollbar.pack(side="right", fill="y")
        self.pw.config(yscrollcommand=pw_scrollbar.set)
        pw_text_frame.grid(row=1, column=0, sticky="nsew")
        
        check_button_frame = ttk.Frame(pw_frame)
        check_button_frame.grid(row=1, column=1, sticky="ns", padx=(6,0))
        ttk.Button(check_button_frame, text=_("Tjek kodeliste"), image=self.icons.get('check'), compound="top", command=self._start_check_only).pack(expand=True, fill="x", pady=(0, 2))
        ttk.Button(check_button_frame, text=_("Gæt kodeord"), image=self.icons.get('guess'), compound="top", command=self._start_guessing).pack(expand=True, fill="x")

        action_frame = ttk.LabelFrame(pwf, text=_("Forside"), padding=6)
        action_frame.grid(row=0, column=1, sticky="nsew", padx=(8,0), pady=(0, 6))
        action_frame.columnconfigure(0, weight=1)
        headline_cb = ttk.Checkbutton(action_frame, text=_("Tilføj forside med overskrift"), variable=self.add_headline_var, command=self._toggle_headline_entry)
        headline_cb.grid(row=0, column=0, columnspan=2, sticky="w")
        
        headline_text_frame = ttk.Frame(action_frame)
        self.headline_entry = tk.Text(headline_text_frame, height=6, width=35, state="disabled")
        self.headline_entry.pack(side="left", fill="both", expand=True)
        headline_scrollbar = ttk.Scrollbar(headline_text_frame, orient="vertical", command=self.headline_entry.yview)
        headline_scrollbar.pack(side="right", fill="y")
        self.headline_entry.config(yscrollcommand=headline_scrollbar.set)
        headline_text_frame.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))
        
        ttk.Label(self, text=f"Version {__version__}  ·  2025  ·  Bo Sundgaard  (Copyright)", font=("Segoe UI", 9, "italic"), justify="left").grid(row=3, column=0, sticky="w", padx=8, pady=(0, 6))

    def _cancel_drag(self, event=None):
        """Annullerer en igangværende træk-operation."""
        if self._drag_window:
            self._drag_window.destroy()
            self._drag_window = None
        
        self._drop_indicator.place_forget()
        
        if self._drag_data.get('selection'):
            for iid in self._drag_data['selection']:
                try:
                    if self.tree.exists(iid):
                        self.tree.item(iid, tags=())
                except tk.TclError:
                    pass
        self._drag_data = {}

    def _on_drag_start(self, event):
        """Registrerer startpositionen for en mulig træk-operation."""
        iid = self.tree.identify_row(event.y)
        if iid and iid in self.tree.selection():
            self._drag_data = {
                'iid': iid,
                'selection': self.tree.selection(),
                'start_x': event.x,
                'start_y': event.y,
            }
        else:
            self._drag_data = {}

    def _on_drag_motion(self, event):
        """Håndterer visuel feedback under træk."""
        if not self._drag_data.get('selection'): return

        if not self._drag_window:
            dx = abs(event.x - self._drag_data['start_x'])
            dy = abs(event.y - self._drag_data['start_y'])
            if dx > 5 or dy > 5:
                self._create_drag_window()
                for iid in self._drag_data['selection']:
                    self.tree.item(iid, tags=('drag_selection',))
        if self._drag_window:
            self._drag_window.geometry(f"+{event.x_root + 20}+{event.y_root + 10}")
            self._update_drop_indicator(event)

    def _update_drop_indicator(self, event):
        """Opdaterer positionen af den blå drop-markør."""
        target_iid = self.tree.identify_row(event.y)
        
        if not target_iid:
            all_iids = self.tree.get_children()
            if all_iids:
                last_iid = all_iids[-1]
                x, y, w, h = self.tree.bbox(last_iid)
                self._drop_indicator.place(x=x, y=y + h, width=w)
        else:
            x, y, w, h = self.tree.bbox(target_iid)
            self._drop_indicator.place(x=x, y=y, width=w)

    def _on_drag_drop(self, event):
        """Udfører flytningen, hvis det var et reelt træk, og rydder op."""
        if not self._drag_window:
            self._cancel_drag()
            return

        items_to_move = self._drag_data.get('selection')
        self._cancel_drag()
        if not items_to_move: return
        
        all_iids = list(self.tree.get_children())
        drop_target_iid = self.tree.identify_row(event.y)
        
        remaining_iids = [i for i in all_iids if i not in items_to_move]

        if drop_target_iid and drop_target_iid not in items_to_move:
            insert_index = remaining_iids.index(drop_target_iid)
        else:
            insert_index = len(remaining_iids)
        
        final_order = remaining_iids[:insert_index] + list(items_to_move) + remaining_iids[insert_index:]

        for index, iid in enumerate(final_order):
            self.tree.move(iid, "", index)

        self.tree.selection_set(items_to_move)
        
    def _create_drag_window(self):
        """Opretter et halvgennemsigtigt overlay-vindue for træk."""
        if self._drag_window: return
        
        selection = self._drag_data.get('selection')
        if not selection: return

        self._drag_window = tk.Toplevel(self)
        self._drag_window.overrideredirect(True)
        self._drag_window.attributes('-topmost', True)
        self._drag_window.attributes('-alpha', 0.7)
        
        first_iid = selection[0]
        thumbnail = self.thumbnail_cache.get(first_iid)

        if thumbnail:
            container = ttk.Frame(self._drag_window, relief="solid", borderwidth=1)
            container.pack()
            lbl = ttk.Label(container, image=thumbnail)
            lbl.pack()

            if len(selection) > 1:
                badge = ttk.Label(lbl, text=f"+{len(selection) - 1}", background="red", foreground="white", padding=(2, 1))
                badge.place(relx=1.0, rely=0.0, anchor='ne')
        else:
            lbl = ttk.Label(self._drag_window, text=f"{len(selection)} fil(er)", background="lightgrey", padding=10, relief="solid", borderwidth=1)
            lbl.pack()

    def _create_context_menu(self):
        self.context_menu = tk.Menu(self, tearoff=0)
        self.context_menu.add_command(label=_("Vis / rediger"), command=self._open_first_selected_preview)
        self.context_menu.add_separator()
        self.context_menu.add_command(label=_("Roter venstre"), command=self._rotate_left)
        self.context_menu.add_command(label=_("Roter højre"), command=self._rotate_right)
        self.context_menu.add_command(label=_("Split sider"), command=self._split_pdf)
        self.context_menu.add_separator()
        self.context_menu.add_command(label=_("Slet side(er)"), command=self._remove)

    def _show_context_menu(self, event):
        self._cancel_drag()
        iid = self.tree.identify_row(event.y)
        if iid and iid not in self.tree.selection():
            self.tree.selection_set(iid)
            self.tree.focus(iid)
        
        selection = self.tree.selection()
        if not selection:
            return

        num_selected = len(selection)
        is_single_selection = (num_selected == 1)
        
        self.context_menu.entryconfig("Vis / rediger", state="normal" if is_single_selection else "disabled")
        
        if is_single_selection and Path(self.paths[selection[0]]).suffix.lower() == '.pdf':
            self.context_menu.entryconfig("Split sider", state="normal")
        else:
            self.context_menu.entryconfig("Split sider", state="disabled")

        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def _on_shift_arrow_select(self, event):
        if not self.tree.selection(): return "break"

        focused = self.tree.focus()
        if event.keysym == 'Down':
            item_to_select = self.tree.next(focused)
        else:
            item_to_select = self.tree.prev(focused)
        
        if item_to_select:
            self.tree.selection_add(item_to_select)
            self.tree.focus(item_to_select)
            self.tree.see(item_to_select)
        
        return "break"

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
    
    def _on_pw_text_modified(self, event=None):
        if not self.pw.edit_modified():
            return
        self._pw_cache = None
        self.pw.edit_modified(False)

    def _pwlist(self) -> list[str]:
        return list(dict.fromkeys(l.strip() for l in self.pw.get("1.0", "end").splitlines() if l.strip()))
    
    def _add_paths(self, paths):
        pw_list = self._get_all_passwords()
        for p in paths:
            if p in self.paths.values(): continue
            enc_key = pdf_utils.enc_status(p, pw_list)
            values = (
                Path(p).name,
                pdf_utils.get_page_count(p, pw_list),
                self._enc_status_label(enc_key),
                pdf_utils.get_creation_date(p, pw_list),
                enc_key,
            )
            iid = self.tree.insert("", "end", text="...", values=values)
            self.paths[iid] = p
            self.thumbnail_manager.queue_request(p, iid)

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
        pw_list = self._get_all_passwords()
        for p in paths:
            if not os.path.exists(p):
                continue
            if Path(p).suffix.lower() not in self._SUPPORTED_DROP_EXTENSIONS:
                continue
            if p in self.paths.values():
                continue
            enc_key = pdf_utils.enc_status(p, pw_list)
            values = (
                Path(p).name,
                pdf_utils.get_page_count(p, pw_list),
                self._enc_status_label(enc_key),
                pdf_utils.get_creation_date(p, pw_list),
                enc_key,
            )
            iid = self.tree.insert("", "end", text="...", values=values)
            self.paths[iid] = p
            self.thumbnail_manager.queue_request(p, iid)
        return event.action

    def _on_drop_enter(self, event):
        self.tree.configure(style="DropTarget.Treeview")
        return event.action

    def _on_drop_leave(self, event):
        self._clear_drop_highlight()
        return event.action

    def _clear_drop_highlight(self):
        self.tree.configure(style="Treeview")

    def _update_tree_item_thumbnail(self, iid: str, pil_image: Image.Image | None):
        """Build the PhotoImage on the main thread and apply it to the tree.

        Workers hand back a PIL image (or None) — Tk objects must only be created
        on the main thread, so PhotoImage construction and the encrypted-status
        check happen here. This is what makes the parallel worker pool safe.
        """
        try:
            # Double-check that the item still exists and is valid
            if (not self.tree.exists(iid) or
                    iid not in self.paths or
                    self.thumbnail_manager._stop_thumbnail_workers.is_set()):
                return

            # Still-encrypted files get the lock placeholder. Read the stable
            # EncKey column (never the localized visible "Krypteret" column).
            if self.tree.set(iid, "EncKey") == pdf_utils.ENC_ENCRYPTED:
                photo = self.thumbnail_manager._get_encrypted_placeholder((50, 50))
            elif pil_image is not None:
                photo = ImageTk.PhotoImage(pil_image)
            else:
                return  # nothing renderable and not flagged encrypted

            # Store in cache first, then update the tree item atomically
            self.thumbnail_cache[iid] = photo
            self.tree.item(iid, image=photo, text="")

        except (tk.TclError, RuntimeError) as e:
            # Item was deleted or tree was destroyed
            logger.error("Thumbnail update failed for %s: %s", iid, e)
            # Clean up thumbnail from cache if it exists
            self.thumbnail_cache.pop(iid, None)
    
    def _remove(self, iids_to_remove: list[str] | None = None):
        if not iids_to_remove:
            iids_to_remove = list(self.tree.selection())
        for iid in iids_to_remove:
            if iid in self.open_previews and self.open_previews[iid].winfo_exists():
                self.open_previews[iid].destroy()
            self.paths.pop(iid, None)
            self.thumbnail_cache.pop(iid, None)
            self.rotations.pop(iid, None)
            self.croppings.pop(iid, None)
            self.tree.delete(iid)

    def _remove_selected_from_event(self, event=None):
        """Wrapper to call _remove for key binding events."""
        self._remove()
    
    def _rotate_left(self):
        self._rotate_selected(-90)

    def _rotate_right(self):
        self._rotate_selected(90)
    
    def _rotate_selected(self, angle: int):
        for iid in self.tree.selection():
            current_rotation = self.rotations.get(iid, 0)
            self.rotations[iid] = (current_rotation + angle + 360) % 360
            self.croppings.pop(iid, None)
            # Use thumbnail queue instead of individual threads (Fix for Bug 1)
            self.thumbnail_manager.queue_request(self.paths[iid], iid)
            if iid in self.open_previews and self.open_previews[iid].winfo_exists():
                self.open_previews[iid]._update_display()

    def _open_preview(self, iid: str):
        if not iid: return
        if iid in self.open_previews and self.open_previews[iid].winfo_exists():
            self.open_previews[iid].lift()
            return

        main_w, main_h = self.winfo_width(), self.winfo_height()
        preview_w, preview_h = int(main_w * 0.8), int(main_h * 0.8)
        
        preview = PreviewWindow(master=self, iid=iid, size=(preview_w, preview_h))
        self.open_previews[iid] = preview
        preview.bind("<Destroy>", lambda e, item_id=iid: self.open_previews.pop(item_id, None))

    def _on_double_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not self._drag_data.get('selection'):
            self._open_preview(iid)
    
    def _open_first_selected_preview(self):
        selection = self.tree.selection()
        if len(selection) == 1:
            self._open_preview(selection[0])

    def _up(self):
        for iid in sorted(self.tree.selection(), key=self.tree.index):
            if (prev := self.tree.prev(iid)):
                self.tree.move(iid, "", self.tree.index(prev))

    def _down(self):
        for iid in sorted(self.tree.selection(), key=self.tree.index, reverse=True):
            if (nxt := self.tree.next(iid)):
                self.tree.move(iid, "", self.tree.index(nxt))
    
    def _reverse(self):
        selected_iids = self.tree.selection()
        if len(selected_iids) > 1:
            all_iids = list(self.tree.get_children())
            reversed_selection_iter = reversed(selected_iids)
            new_order = []
            for iid in all_iids:
                if iid in selected_iids:
                    new_order.append(next(reversed_selection_iter))
                else:
                    new_order.append(iid)
            for idx, iid in enumerate(new_order):
                self.tree.move(iid, "", idx)
        else:
            all_iids = self.tree.get_children()
            for idx, iid in enumerate(reversed(all_iids)):
                self.tree.move(iid, "", idx)

    def _move_top(self):
        for idx, iid in enumerate(sorted(self.tree.selection(), key=self.tree.index)):
            self.tree.move(iid, "", idx)

    def _move_bottom(self):
        for iid in sorted(self.tree.selection(), key=self.tree.index):
            self.tree.move(iid, "", 'end')
    
    def _refresh_all(self):
        pw_list = self._get_all_passwords()
        for iid in self.tree.get_children():
            p = self.paths[iid]
            # Performance: single PDF open via get_pdf_metadata() instead of 3 separate opens
            meta = pdf_utils.get_pdf_metadata(p, pw_list)
            self.tree.set(iid, "Sider", meta["page_count"])
            self.tree.set(iid, "Krypteret", self._enc_status_label(meta["enc_status"]))
            self.tree.set(iid, "EncKey", meta["enc_status"])
            self.tree.set(iid, "CreationDate", meta["creation_date"])
            # Use thumbnail queue instead of individual threads (Fix for Bug 1)
            self.thumbnail_manager.queue_request(p, iid)
    
    def _sort_column(self, col):
        if col == "Sider":
            data = [(int(self.tree.set(iid, col) or 0), iid) for iid in self.tree.get_children("")]
        else:
            data = [(self.tree.set(iid, col), iid) for iid in self.tree.get_children("")]
            if col == "CreationDate":
                data = [(datetime.datetime.strptime(v, "%Y-%m-%d") if v else datetime.datetime.min, i) for v, i in data]
        
        rev = self._sort_reverse[col]
        data.sort(key=lambda x: x[0], reverse=not rev)
        for idx, (_, iid) in enumerate(data):
            self.tree.move(iid, "", idx)
        self._sort_reverse[col] = not rev
    
    def _split_pdf(self):
        selected_iids = self.tree.selection()
        if len(selected_iids) != 1:
            messagebox.showinfo(_("Split PDF"), _("Vælg venligst én PDF-fil for at splitte den."))
            return

        iid = selected_iids[0]
        file_path = self.paths[iid]
        if Path(file_path).suffix.lower() != '.pdf':
            messagebox.showinfo(_("Split PDF"), _("Kun PDF-filer kan splittes."))
            return

        pw_list = self._get_all_passwords()
        page_count_str = pdf_utils.get_page_count(file_path, pw_list)
        if not page_count_str or int(page_count_str) <= 1:
            messagebox.showinfo(_("Split PDF"), _("Denne PDF har kun én side og kan ikke splittes."))
            return

        split_dir = utils.get_app_data_path(f"split_cache_{uuid.uuid4().hex}")
        p_widgets = self._show_progress_dialog(_("Splitter PDF"), _("Forbereder at splitte %s sider...") % page_count_str)
        threading.Thread(target=self._split_pdf_worker, args=(iid, file_path, pw_list, p_widgets, split_dir), daemon=True).start()

    def _split_pdf_worker(self, original_iid: str, file_path: str, pw_list: list[str], p_widgets: dict, split_dir: Path):
        try:
            source_pdf = pdf_utils.open_with_passwords(file_path, pw_list)
            if not source_pdf:
                raise RuntimeError(_("Kunne ikke åbne PDF'en (måske forkert adgangskode?)."))

            try:
                total = len(source_pdf.pages)
                original_stem = Path(file_path).stem
                new_paths = []

                for i, page in enumerate(source_pdf.pages):
                    self._queue.put((self._update_save_progress, (p_widgets, i + 1, total)))
                    # Luk hver ny Pdf igen — ellers holdes filhandles/hukommelse
                    # (og dermed kildefilen) åbne indtil processen lukker.
                    with Pdf.new() as new_pdf:
                        new_pdf.pages.append(page)
                        new_filename = f"{original_stem}_side_{i+1}.pdf"
                        new_path = split_dir / new_filename
                        new_pdf.save(new_path)
                    new_paths.append(str(new_path))
            finally:
                source_pdf.close()

            self._queue.put((self._split_pdf_complete, (original_iid, new_paths, p_widgets)))
        except Exception as e:
            err = str(e)
            def show_split_error(w):
                w['window'].destroy()
                messagebox.showerror(_("Fejl ved split"), _("Der opstod en fejl:\n%s") % err)
            self._queue.put((show_split_error, (p_widgets,)))

    def _split_pdf_complete(self, original_iid: str, new_paths: list[str], p_widgets: dict):
        if not self.tree.exists(original_iid):
            p_widgets['window'].destroy()
            return
                    
        insert_index = self.tree.index(original_iid)
        self._remove([original_iid])
        
        pw_list = self._get_all_passwords()
        for i, p_str in enumerate(new_paths):
            p = Path(p_str)
            enc_key = pdf_utils.enc_status(str(p), pw_list)
            values = (
                p.name,
                pdf_utils.get_page_count(str(p), pw_list),
                self._enc_status_label(enc_key),
                pdf_utils.get_creation_date(str(p), pw_list),
                enc_key,
            )
            new_iid = self.tree.insert("", insert_index + i, text="...", values=values)
            self.paths[new_iid] = str(p)
            # Use thumbnail queue instead of individual threads (Fix for Bug 1)
            self.thumbnail_manager.queue_request(str(p), new_iid)

        p_widgets['window'].destroy()

    def _save_dec(self):
        pdf_iids = [i for i in self.tree.get_children() if self.tree.set(i, "EncKey") in (pdf_utils.ENC_DECRYPTED, pdf_utils.ENC_NOT_ENCRYPTED)]
        if not pdf_iids:
            messagebox.showinfo(_("Gem"), _("Ingen PDF-filer at gemme."))
            return

        dest_folder = filedialog.askdirectory(title=_("Vælg mappe til at gemme filer"))
        if not dest_folder:
            return

        total = len(pdf_iids)
        # Snapshot (path, enc_key) on the main thread so the worker never touches widgets.
        files = [(self.paths[i], self.tree.set(i, "EncKey")) for i in pdf_iids]
        p_widgets = self._show_progress_dialog(_("Gemmer PDF'er"), _("Forbereder at gemme %s filer...") % total)
        threading.Thread(target=self._save_dec_worker, args=(files, self._get_all_passwords(), p_widgets, total, dest_folder), daemon=True).start()

    @staticmethod
    def _unique_save_path(out: Path, reserved: set[str]) -> Path:
        """Avoid overwriting a source/input file (in `reserved`) or an existing file.
        Appends _1, _2, ... to the stem until the path is free."""
        candidate = out
        counter = 1
        while os.path.normcase(os.path.abspath(candidate)) in reserved or candidate.exists():
            candidate = out.with_name(f"{out.stem}_{counter}{out.suffix}")
            counter += 1
        return candidate

    def _save_dec_worker(self, files, pw_list, p_widgets, total, dest_folder):
        saved = 0
        # Normalized set of input paths — never overwrite one of these.
        source_paths = {os.path.normcase(os.path.abspath(src)) for src, _key in files}
        for i, (src, enc_key) in enumerate(files):
            self._queue.put((self._update_save_progress, (p_widgets, i + 1, total)))
            if not (pdf := pdf_utils.open_with_passwords(src, pw_list)): continue

            original_path = Path(src)
            if enc_key == pdf_utils.ENC_DECRYPTED:
                new_filename = original_path.stem + "_dekrypteret.pdf"
            else:
                new_filename = original_path.name
            out = self._unique_save_path(Path(dest_folder) / new_filename, source_paths)

            try:
                pdf.save(out)
                saved += 1
            except Exception as e:
                logger.error("Fejl ved gemning af %s: %s", original_path.name, e)
        self._queue.put((self._save_dec_complete, (saved, p_widgets)))

    def _save_dec_complete(self, saved_count, p_widgets):
        p_widgets['window'].destroy()
        if saved_count > 0:
            messagebox.showinfo(_("Gem"), _("%s PDF-filer gemt.") % saved_count)
    
    def _merge(self):
        if not self.tree.get_children():
            return messagebox.showwarning(_("Ingen filer"), _("Tilføj filer først"))
        out_path = filedialog.asksaveasfilename(defaultextension=".pdf", initialfile="Flettet.pdf", filetypes=[(_("PDF-filer"), "*.pdf")])
        if not out_path: return
        total = len(self.tree.get_children())
        p_widgets = self._show_progress_dialog(_("Fletter PDF'er"), _("Forbereder at flette %s filer...") % total)
        threading.Thread(target=self._merge_worker, args=(out_path, self._get_all_passwords(), p_widgets, total), daemon=True).start()

    def _merge_worker(self, out_path, pw_list, p_widgets, total):
        merged, ok, fail = Pdf.new(), 0, 0
        if self.add_headline_var.get():
            headline = self.headline_entry.get("1.0", 'end-1c').strip()
            if headline:
                if title_pdf_buffer := self._create_title_page_pdf(headline):
                    with pikepdf.open(title_pdf_buffer) as title_src:
                        merged.pages.extend(title_src.pages)
                else:
                    fail += 1
        
        all_iids = self.tree.get_children()
        for i, iid in enumerate(all_iids):
            self._queue.put((self._update_save_progress, (p_widgets, i + 1, total)))
            current_path = self.paths[iid]
            path_obj = Path(current_path)
            file_ext = path_obj.suffix.lower()
            angle = self.rotations.get(iid, 0)
            crop = self.croppings.get(iid)
            pdf_to_add = None
            try:
                if file_ext == '.pdf':
                    pdf_to_add = pdf_utils.open_with_passwords(current_path, pw_list)
                    if pdf_to_add and angle != 0:
                        for page in pdf_to_add.pages:
                            page.Rotate = (page.get('/Rotate', 0) + angle) % 360
                elif file_ext in ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'):
                    pdf_buffer = pdf_utils._image_to_pdf_a4(current_path, angle, crop)
                    pdf_to_add = pikepdf.open(pdf_buffer) if pdf_buffer else None
                if pdf_to_add:
                    merged.pages.extend(pdf_to_add.pages)
                    pdf_to_add.close()
                    ok += 1
                else:
                    fail += 1
                    with Pdf.open(utils.create_error_pdf(path_obj.name)) as err_src:
                        merged.pages.extend(err_src.pages)
            except Exception as e:
                logger.error("Fejl ved behandling af fil %s: %s", path_obj.name, e)
                fail += 1
                with Pdf.open(utils.create_error_pdf(path_obj.name)) as err_src:
                    merged.pages.extend(err_src.pages)
        
        try:
            merged.save(out_path)
            self._queue.put((self._merge_complete, (ok, fail, out_path, p_widgets)))
        except Exception as e:
            self._queue.put((self._merge_error, (e, p_widgets)))

    def _merge_complete(self, ok, fail, out_path, p_widgets):
        p_widgets['window'].destroy()
        messagebox.showinfo(
            _("Fletning fuldført"),
            _("PDF gemt som:\n%(path)s\n\n%(ok)s filer behandlet korrekt\n%(fail)s filer fejlede")
            % {"path": out_path, "ok": ok, "fail": fail},
        )

    def _merge_error(self, error, p_widgets):
        p_widgets['window'].destroy()
        messagebox.showerror(_("Fejl"), _("Kunne ikke gemme filen:\n%s") % error)

    def _start_check_only(self):
        items = [i for i in self.tree.get_children() if self.tree.set(i, "EncKey") == pdf_utils.ENC_ENCRYPTED]
        if not items:
            return messagebox.showinfo(_("Tjek kodeliste"), _("Der er ingen krypterede PDF-filer på listen."))
        self._stop_guessing = False
        self._stop_event = threading.Event()
        win = tk.Toplevel(self)
        win.title(_("Tjekker kodeord fra liste"))
        win.geometry("500x500")
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
        canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
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
                self.thumbnail_manager.queue_request(self.paths[iid], iid)
            else:
                self._queue.put((self._update_status, (iid, _("Ikke fundet"))))

            self._queue.put((self._stop_guess_pb, (iid,)))
            if not self._stop_guessing:
                time.sleep(0.05)

    def _start_guessing(self):
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
            self._run_guessing_process(modes, max_len)
            
        ttk.Button(btn_frame, text=_("Start"), command=start_action).pack(side="left", padx=5)
        ttk.Button(btn_frame, text=_("Annuller"), command=dialog.destroy).pack(side="left", padx=5)

    def _run_guessing_process(self, modes, max_len):
        items = [i for i in self.tree.get_children() if self.tree.set(i, "EncKey") == pdf_utils.ENC_ENCRYPTED]
        if not items:
            return messagebox.showinfo(_("Gæt kodeord"), _("Der er ingen krypterede PDF-filer på listen."))
            
        self._stop_guessing = False
        self._stop_event = threading.Event()
        win = tk.Toplevel(self)
        win.title(_("Gæt / Tjek kodeord"))
        win.geometry("500x500")
        
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
        canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        
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
        if p in self._pwlist():
            return
        txt = self.pw.get("1.0", "end-1c")
        self.pw.insert("end", ("\n" if txt and not txt.endswith("\n") else "") + p + "\n")
    
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
                self.thumbnail_manager.queue_request(self.paths[iid], iid)
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

        settings_win.language_changed = False
        
        # Position settings window relative to main window - centered towards top right
        settings_win.update_idletasks()  # Ensure window dimensions are calculated
        main_x = self.winfo_x()
        main_y = self.winfo_y()
        main_width = self.winfo_width()
        settings_win_width = 400  # Approximate width
        settings_win_height = 530  # Approximate height
        
        # Position in upper right area of main window
        x = main_x + main_width - settings_win_width - 50
        y = main_y + 50
        settings_win.geometry(f"{settings_win_width}x{settings_win_height}+{x}+{y}")

        # Password Length Setting
        pw_len_frame = ttk.LabelFrame(settings_win, text=_("Kodeordsgætning"), padding=10)
        pw_len_frame.pack(padx=10, pady=5, fill="x")

        pw_len_label = ttk.Label(pw_len_frame, text=_("Maksimal længde på kodeord der skal gættes:"))
        pw_len_label.grid(row=0, column=0, sticky="w", pady=2)
        
        current_max_len = self.config.getint("Security", "bruteforce_max_len", fallback=5)
        self.max_len_var = tk.IntVar(value=current_max_len)
        
        max_len_spinbox = ttk.Spinbox(pw_len_frame, from_=1, to=10, textvariable=self.max_len_var, width=5)
        max_len_spinbox.grid(row=0, column=1, sticky="ew", padx=5, pady=2)
        
        def save_max_len():
            try:
                new_max_len = self.max_len_var.get()
                if 1 <= new_max_len <= 10:
                    self.config.set("Security", "bruteforce_max_len", str(new_max_len))
                    self.config.save()
                    self._show_custom_dialog(_("Indstillinger"), _("Maksimal kodeordslængde er gemt."), parent_window=settings_win)
                else:
                    self._show_custom_dialog(_("Fejl"), _("Længden skal være mellem 1 og 10."), "error", parent_window=settings_win)
            except ValueError:
                self._show_custom_dialog(_("Fejl"), _("Ugyldig værdi for længde."), "error", parent_window=settings_win)
        
        save_len_button = ttk.Button(pw_len_frame, text=_("Gem længde"), command=save_max_len)
        save_len_button.grid(row=1, column=0, columnspan=2, pady=5)

        # Language Setting
        lang_frame = ttk.LabelFrame(settings_win, text=_("Sprog"), padding=10)
        lang_frame.pack(padx=10, pady=5, fill="x")

        lang_label = ttk.Label(lang_frame, text=_("Vælg sprog:"))
        lang_label.grid(row=0, column=0, sticky="w", pady=2)
        
        language_names = LocalizationManager.get_supported_languages()
        self.language_var = tk.StringVar(value=LocalizationManager.get_language_name(self.language_code))
        
        lang_combobox = ttk.Combobox(lang_frame, textvariable=self.language_var, values=language_names, state="readonly")
        lang_combobox.grid(row=0, column=1, sticky="ew", padx=5, pady=2)

        def save_language():
            selected_language_name = self.language_var.get()
            lang_code = LocalizationManager.get_language_code(selected_language_name)
            
            if lang_code == self.language_code:
                return

            self.config.set("General", "language", lang_code)
            self.config.save()
            
            # Mark that language has changed
            settings_win.language_changed = True
            
            self._show_custom_dialog(
                _("Sprog gemt"), 
                _("Sprogindstillingen er gemt. Ændringerne vil blive anvendt, når du lukker dette vindue."), 
                parent_window=settings_win
            )

        save_lang_button = ttk.Button(lang_frame, text=_("Gem sprog"), command=save_language)
        save_lang_button.grid(row=1, column=0, columnspan=2, pady=5)

        # Clear Cached Passwords Button
        cache_frame = ttk.LabelFrame(settings_win, text=_("Cache"), padding=10)
        cache_frame.pack(padx=10, pady=5, fill="x")

        clear_cache_button = ttk.Button(cache_frame, text=_("Slet gemte kodeord"), command=self._clear_password_cache)
        clear_cache_button.pack(pady=5)

        ctx_frame = ttk.LabelFrame(settings_win, text=_("Windows integration"), padding=10)
        ctx_frame.pack(padx=10, pady=5, fill="x")

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

        ttk.Checkbutton(
            ctx_frame,
            text=_("Tilføj 'Flet med UniteDocs' til højreklik-menu for PDF-filer"),
            variable=ctx_var,
            command=_toggle_ctx,
        ).pack(anchor="w")

        # Log Setting
        log_frame = ttk.LabelFrame(settings_win, text=_("Log"), padding=10)
        log_frame.pack(padx=10, pady=5, fill="x")

        ttk.Label(
            log_frame,
            text=_("Logfilen kan hjælpe med fejlfinding."),
            wraplength=360,
            justify="left",
        ).pack(anchor="w", pady=(0, 6))

        def _make_link(parent, text, command):
            link = ttk.Label(
                parent,
                text=text,
                foreground="#0066CC",
                cursor="hand2",
                font=("Segoe UI", 9, "underline"),
            )
            link.bind("<Button-1>", lambda _e: command())
            return link

        _make_link(log_frame, _("Download log"),
                   lambda: self._download_log(settings_win)).pack(anchor="w", pady=1)
        _make_link(log_frame, _("Send log via e-mail"),
                   lambda: self._email_log(settings_win)).pack(anchor="w", pady=1)

        def on_settings_close():
            if settings_win.language_changed:
                # Update language settings before rebuilding
                new_lang_code = self.config.get("General", "language", fallback="en")
                self.language_code = new_lang_code
                # Update global _ function in main module and preview_window module
                global _
                # Update global _ function in main module
                global _
                LocalizationManager.get_instance().set_language(new_lang_code)
                _ = LocalizationManager.get_text
                
                # PreviewWindow uses LocalizationManager.get_text internally, so no need to inject anything.
                self._rebuild_ui_for_language_change()
            settings_win.destroy()

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
        # Gem den nuværende tilstand
        current_paths = list(self.paths.items())
        current_rotations = self.rotations.copy()
        current_croppings = self.croppings.copy()
        current_pw_text = self.pw.get("1.0", "end-1c")
        current_headline_text = self.headline_entry.get("1.0", "end-1c")
        is_headline_enabled = self.add_headline_var.get()

        # Nulstil UI-referencer først (før widgets ødelægges)
        self._drag_data = {}
        if self._drag_window:
            self._drag_window.destroy()
        self._drag_window = None
        self._drop_indicator = None

        # Ryd eksisterende widgets
        for widget in self.winfo_children():
            widget.destroy()

        # Nulstil tilstande, der påvirkes af UI-genopbygning
        self.paths.clear()
        self.thumbnail_cache.clear()
        self.rotations.clear()
        self.croppings.clear()
        self.open_previews.clear()

        # Genindlæs UI-komponenter
        self._load_icons()
        self._build_ui()

        # Gendan den gemte tilstand
        self.pw.insert("1.0", current_pw_text)
        self.headline_entry.insert("1.0", current_headline_text)
        self.add_headline_var.set(is_headline_enabled)
        self._toggle_headline_entry()

        pw_list = self._get_all_passwords()
        for old_iid, p in current_paths:
            enc_key = pdf_utils.enc_status(p, pw_list)
            values = (
                Path(p).name,
                pdf_utils.get_page_count(p, pw_list),
                self._enc_status_label(enc_key),
                pdf_utils.get_creation_date(p, pw_list),
                enc_key,
            )
            new_iid = self.tree.insert("", "end", text="...", values=values)
            self.paths[new_iid] = p
            if old_iid in current_rotations:
                self.rotations[new_iid] = current_rotations[old_iid]
            if old_iid in current_croppings:
                self.croppings[new_iid] = current_croppings[old_iid]
            
            # Use thumbnail queue instead of individual threads (Fix for Bug 1)
            self.thumbnail_manager.queue_request(p, new_iid)

    def _clear_password_cache(self):
        cache_path = self._get_password_cache_path()
        if cache_path.exists():
            try:
                os.remove(cache_path)
                messagebox.showinfo(_("Cache slettet"), _("Alle gemte kodeord er slettet."))
            except Exception as e:
                messagebox.showerror(_("Fejl"), _("Kunne ikke slette cache: %s") % e)
        else:
            messagebox.showinfo(_("Cache"), _("Ingen kodeordscache fundet."))