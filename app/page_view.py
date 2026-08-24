"""Page view (Fase 3): pages as first-class tiles, with a **virtualized** grid.

Left: a scrollable panel of page thumbnails grouped per source file. Right: a
large preview of the selected page.

Virtualization is the key to staying responsive on big documents: the layout of
every page is computed as pure geometry (cheap), but Tk widgets are created ONLY
for the tiles currently in the viewport (plus a small buffer) and destroyed as
they scroll out. Creating 1500 widgets up front for a 375-page file froze the UI
for ~8 s; virtualized, only ~20-40 widgets ever exist at once.

Rendering goes through :class:`~app.page_render.PageRenderManager` (workers never
touch Tk) and is size-driven (see pdf_renderer). Only visible tiles are queued.

Fase 3 is read-only; per-tile editing / context menu arrive in later phases.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk, simpledialog, colorchooser
import os
from pathlib import Path
import datetime
import tkinter.font as tkfont

import threading

import pymupdf
from PIL import Image, ImageTk

from . import edit_model as em
from . import pdf_utils
from . import pdf_renderer
from . import page_render
from . import annotations as an
from . import redaction
from . import theme
from . import icons_vector
from .tooltip import Tooltip
from .view_transform import ViewTransform
from .page_canvas import PageCanvas
from .logging_config import get_logger

logger = get_logger(__name__)

# Vaerktoej -> (ikonnavn, tooltip). Raekkefoelgen bestemmer bar-layoutet;
# tuplerne grupperes af _TOOL_GROUPS nedenfor. redact/redact_text farves altid
# roede (destruktive) -- se _tool_color().
_REDACT_TOOLS = ("redact", "redact_text")

TILE_IMG = (110, 140)          # rendered image box inside a tile
TILE_PAD = 6
NUM_H = 18                     # page-number strip
TILE_OUTER_W = TILE_IMG[0] + 2 * TILE_PAD
TILE_OUTER_H = TILE_IMG[1] + NUM_H + 2 * TILE_PAD
CELL_W = TILE_OUTER_W + 10     # grid cell incl. spacing
CELL_H = TILE_OUTER_H + 10
HEADER_H = 46      # to linjer navn/dato + luft
PAD = 8
GROUP_GAP = 22     # ogsaa udtraeks-zonen ved traek-og-slip
# Background prefetch: once pages are known, warm the thumbnail cache at low
# priority so scrolling is instant. Capped so a huge document doesn't render
# thousands of pages up front (the LRU cache would evict them anyway).
PREFETCH_CAP = 400

# Fliser: i hvile en diskret kant, ved valg et LET accent-fyld + 2px accent-ring
# (ikke det gamle massive blaa felt). Alle vaerdier fra theme.C -- ingen hex her.
_TILE_BG = theme.C["tile_bg"]
_TILE_RING = theme.C["border"]
_SELECT_FILL = theme.C["accent_subtle"]
_SELECT_RING = theme.C["accent"]
_CANVAS_BG = theme.C["bg"]
_DND_BG = theme.C["accent_subtle"]        # drop-highlight paa gitteret
_EMPTY_FG = theme.C["text_muted"]         # "ingen sider"-tekst
_HEADER_BG = theme.C["bg"]                # filhovedets flade i hvile
_DROP_LINE = theme.C["drop_line"]         # indsaetnings-indikator ved traek


class PageView(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.render_mgr = app.page_render_mgr
        self.cells = []            # layout cells (geometry only, no widgets)
        self.page_order = []       # ordered page uids (for keyboard navigation)
        self.total_height = 0
        self.cols = 1
        self.active = {}           # cell_key -> {'win': canvas_item, 'w': dict, 'cell': cell}
        self._render_after = None
        self._layout_after = None
        self._preview_after = None
        self._preview_photo = None
        self._preview_uid = None
        self._selected = []             # ordnede side-uids (multi-markering)
        self._anchor_uid = None         # Shift-omraadets anker
        self._selected_file = None      # fil-iid; udelukker sidemarkering
        self._hdr_font = None           # cachet tkfont.Font til hovedets maalinger
        self._ctx_file = None           # filen en fil-kontekstmenu blev aabnet paa
        self._drag = None               # igangvaerende sidetraek
        self._drag_ghost = None         # halvgennemsigtigt traek-vindue
        self._ghost_photo = None        # dets PhotoImage (skal holdes i live)
        self._dropbar = None            # widget der viser hvor siden lander
        self._autoscroll_after = None
        # Annotation drawing state (Fase 6+).
        self.active_tool = "hand"
        self._tool_var = tk.StringVar(value="hand")
        self._vt = None                 # ViewTransform for the shown preview
        self._preview_geom = None       # (rotate_deg, disp_w, disp_h) snapshot
        self._preview_words = []        # get_text('words') for text-markup tools
        self._preview_delta = 0         # user rotation delta of the shown page
        self._draw = None               # in-progress drawing gesture
        self._anno_color = (1.0, 0.0, 0.0)
        self._anno_width = 2.0
        self._search_var = tk.StringVar()
        self._text_selection = None     # cursor-selected words (right-click target)

        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True)

        left = ttk.Frame(paned)
        self.canvas = tk.Canvas(left, borderwidth=0, highlightthickness=0,
                                background=_CANVAS_BG)
        vsb = ttk.Scrollbar(left, orient="vertical", command=self._on_scrollbar)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel)
        self.canvas.bind("<Button-5>", self._on_mousewheel)
        # Keyboard navigation: next/previous page without clicking thumbnails.
        self.canvas.configure(takefocus=True)
        self.canvas.bind("<Button-1>", lambda e: self.canvas.focus_set())
        self.canvas.bind("<Down>", lambda e: self._navigate(self.cols))
        self.canvas.bind("<Up>", lambda e: self._navigate(-self.cols))
        self.canvas.bind("<Right>", lambda e: self._navigate(1))
        self.canvas.bind("<Left>", lambda e: self._navigate(-1))
        self.canvas.bind("<Next>", lambda e: self._navigate(self._page_step()))     # PageDown
        self.canvas.bind("<Prior>", lambda e: self._navigate(-self._page_step()))   # PageUp
        self.canvas.bind("<Home>", lambda e: self._navigate(-10 ** 9))
        self.canvas.bind("<End>", lambda e: self._navigate(10 ** 9))
        self.canvas.bind("<Delete>", lambda e: self._delete_selected_key())
        paned.add(left, weight=1)

        right = ttk.Frame(paned)
        self._build_anno_toolbar(right)
        self.pcanvas = PageCanvas(right, self.app)
        self.pcanvas.on_zoom_change = self._on_zoom_change
        self.pcanvas.on_page_change = self._highlight_page
        self.pcanvas.pack(fill="both", expand=True)
        paned.add(right, weight=2)

        self.app._register_dnd(self.canvas, on_enter=self._dnd_enter, on_leave=self._dnd_leave)

        # Per-page context menu (Fase 4). Reference items by numeric index at
        # popup time, never by translated label (project rule).
        self._ctx_uid = None
        self._menu = tk.Menu(self, tearoff=0)
        self._menu.add_command(label=_("Roter venstre"), command=lambda: self._rotate_ctx(-90))
        self._menu.add_command(label=_("Roter højre"), command=lambda: self._rotate_ctx(90))
        self._menu.add_separator()
        export_menu = tk.Menu(self._menu, tearoff=0)
        for _fmt, _lbl in (("pdf", "PDF"), ("md", "Markdown"), ("epub", "ePub"),
                           ("jpg", "JPG"), ("png", "PNG")):
            export_menu.add_command(label=_lbl, command=lambda f=_fmt: self._export_ctx(f))
        self._menu.add_cascade(label=_("Eksportér side"), menu=export_menu)
        self._menu.add_separator()
        self._menu.add_command(label=_("Slet side"), command=self._delete_ctx)

        # Kontekstmenu for et FILHOVED.
        self._file_menu = tk.Menu(self, tearoff=0)
        self._file_menu.add_command(label=_("Lås op"), command=self._unlock_file_ctx)
        self._file_ctx_unlock = self._file_menu.index("end")
        self._file_menu.add_command(label=_("Vælg alle sider i filen"),
                                    command=self._select_all_in_file_ctx)
        self._file_menu.add_separator()
        self._file_menu.add_command(label=_("Slet fil"), command=self._delete_file_ctx)

    def _dnd_enter(self, event):
        self.canvas.configure(background=_DND_BG)
        return event.action

    def _dnd_leave(self, event):
        self.canvas.configure(background=_CANVAS_BG)
        return event.action

    # ------------------------------------------------------------------ build
    def rebuild(self):
        """Recompute the (widget-free) layout and refresh the visible widgets.
        Cheap even for thousands of pages — no per-page widgets are created here."""
        self.render_mgr.bump_generation()
        # Drop all active widgets back to nothing (they will be recreated for the
        # new layout's visible range).
        for key in list(self.active):
            self._recycle(key)
        self._compute_layout()
        # Keep a valid selection across rebuilds; auto-select the first page so the
        # preview and keyboard navigation are ready immediately.
        live = set(self.page_order)
        self._selected = [u for u in self._selected if u in live]
        if self._anchor_uid not in live:
            self._anchor_uid = self._selected[-1] if self._selected else None
        if self._selected_file and self.app.model.entry_by_iid(self._selected_file) is None:
            self._selected_file = None
        self._refresh_visible()
        if not self._selected and not self._selected_file and self.page_order:
            self._select_and_reveal(self.page_order[0])
        # Rebuild the continuous preview (stacked pages) from the model.
        if getattr(self, 'pcanvas', None) is not None:
            self.pcanvas.set_document()
        # Warm the cache in the background (low priority) after the visible tiles
        # and preview have had a head start.
        self.after(250, self._prefetch)

    def _prefetch(self):
        """Render pages into the thumbnail cache in the background at low priority,
        so scrolling later is a cache hit. Visible tiles (pri 1) and the preview
        (pri 0) always preempt this (pri 2)."""
        if not self.cells:
            return
        gen = self.render_mgr._current_generation()
        pw = self.app._get_all_passwords()
        count = 0
        for c in self.cells:
            if c["kind"] != "tile":
                continue
            page = c["page"]
            if self.render_mgr.cache.get(self._tile_key(page)) is not None:
                continue
            self.render_mgr.request(
                page.uid, page.src_path, pw, page.src_index, page.rotation,
                TILE_IMG, self._on_tile_ready, priority=2, generation=gen,
                crop=page.crop)
            count += 1
            if count >= PREFETCH_CAP:
                break

    def _compute_layout(self):
        width = max(1, self.canvas.winfo_width())
        self.cols = max(1, (width - 2 * PAD) // CELL_W)
        cells = []
        page_order = []
        y = PAD
        files = self.app.model.files
        self.canvas.delete("empty")
        if not files:
            self.cells = []
            self.page_order = []
            self.canvas.create_text(PAD, PAD, anchor="nw", fill=_EMPTY_FG, tags=("empty",),
                                    text=_("Ingen sider at vise. Tilføj filer."))
            self.total_height = 60
            self.canvas.configure(scrollregion=(0, 0, width, 60))
            return
        for entry in files:
            cells.append({"kind": "header", "key": "h:" + entry.iid,
                          "x": PAD, "y": y, "w": width - 2 * PAD, "h": HEADER_H,
                          "entry": entry})
            y += HEADER_H
            if entry.pages_loaded and entry.pages:
                items = list(entry.pages)
            else:
                items = [None]      # locked placeholder
            for i, page in enumerate(items):
                col, row = i % self.cols, i // self.cols
                x = PAD + col * CELL_W
                cy = y + row * CELL_H
                cells.append({
                    "kind": "tile" if page is not None else "locked",
                    "key": page.uid if page is not None else "lock:" + entry.iid,
                    "x": x, "y": cy, "w": CELL_W, "h": CELL_H,
                    "entry": entry, "page": page,
                    "num": self._page_number(entry, page, i)})
                if page is not None:
                    page_order.append(page.uid)
            nrows = (len(items) + self.cols - 1) // self.cols
            y += nrows * CELL_H + GROUP_GAP
        self.cells = cells
        self.page_order = page_order
        self.total_height = y
        self.canvas.configure(scrollregion=(0, 0, width, y))

    @staticmethod
    def _page_number(entry, page, position):
        """Etiketten under en flise.

        Sider fra filen selv viser deres KILDE-sidetal (``src_index + 1``), så
        man kan se hvad der er slettet. Indsatte sider har ingen kildeside i
        filen — de viser deres plads i listen med et plus foran, så tallene
        ikke kolliderer."""
        if page is None:
            return ""
        if os.path.normcase(page.src_path) == os.path.normcase(entry.path):
            return str(page.src_index + 1)
        return "+%d" % (position + 1)

    def page_number_label(self, uid):
        """Det tal brugeren ser under en side (eller None hvis ukendt)."""
        for c in self.cells:
            if c["kind"] == "tile" and c["key"] == uid:
                return c.get("num")
        return None

    # --------------------------------------------------------------- viewport
    def _visible_range(self, buffer=None):
        h = self.canvas.winfo_height()
        if buffer is None:
            buffer = h                     # one screen of look-ahead each way
        top = self.canvas.canvasy(0) - buffer
        bottom = self.canvas.canvasy(0) + h + buffer
        return top, bottom

    def _refresh_visible(self):
        """Place widgets for the visible range and recycle the rest. Cheap and
        SYNCHRONOUS — called directly on every scroll tick so the grid is always
        drawn (never a blank field). Image *rendering* is throttled separately."""
        self._render_after = None
        if not self.cells:
            return
        top, bottom = self._visible_range()
        needed = {}
        for c in self.cells:
            if c["y"] + c["h"] < top or c["y"] > bottom:
                continue
            needed[c["key"]] = c
        for key in list(self.active):
            if key not in needed:
                self._recycle(key)
        for key, c in needed.items():
            if key not in self.active:
                self._place(c)
            else:
                # Keep position in sync after a relayout (width change).
                a = self.active[key]
                self.canvas.coords(a["win"], c["x"], c["y"])
                a["cell"] = c
        # Ask for the images of whatever settled in view (throttled).
        self._schedule_render_requests()

    def _schedule_render_requests(self):
        """Throttle image requests: during a fast scroll tiles are placed (grid
        visible) but their renders are only requested once scrolling pauses, so the
        render queue is never flooded with pages that scroll straight back out."""
        if getattr(self, "_render_req_after", None):
            self.after_cancel(self._render_req_after)
        self._render_req_after = self.after(50, self._request_visible_renders)

    def _request_visible_renders(self):
        self._render_req_after = None
        gen = self.render_mgr._current_generation()
        pw = self.app._get_all_passwords()
        for key, a in self.active.items():
            cell = a["cell"]
            if cell["kind"] != "tile":
                continue
            page = cell["page"]
            # Sammenlign med den noegle flisens billede blev tegnet FRA. Den gamle
            # test var blot "har flisen et billede?", og den var sand for evigt --
            # saa en roteret eller beskaaret side fik aldrig hentet nyt billede.
            if a.get("photo_key") == self._tile_key(page):
                continue
            self.render_mgr.request(
                page.uid, page.src_path, pw, page.src_index, page.rotation,
                TILE_IMG, self._on_tile_ready, priority=1, generation=gen,
                crop=page.crop)

    # ------------------------------------------------------------------ widgets
    def _place(self, cell):
        if cell["kind"] == "header":
            w = self._make_header(cell)
        elif cell["kind"] == "locked":
            w = self._make_locked(cell)
        else:
            w = self._make_tile(cell)
        win = self.canvas.create_window(cell["x"], cell["y"], anchor="nw",
                                        window=w["holder"], width=cell["w"], height=cell["h"])
        self.active[cell["key"]] = {"win": win, "w": w, "cell": cell}
        if cell["kind"] == "tile":
            # Instant show if already rendered (keeps thumbnails visible during a
            # fast scroll); otherwise a throttled request fills it in.
            page = cell["page"]
            cached = self.render_mgr.cache.get(self._tile_key(page))
            if cached is not None:
                self._set_tile_image(cell["key"], cached)

    def _recycle(self, key):
        a = self.active.pop(key, None)
        if not a:
            return
        try:
            self.canvas.delete(a["win"])
            a["w"]["holder"].destroy()
        except tk.TclError:
            pass

    def _bind_wheel(self, widget):
        """Forward wheel events over a tile/header to the canvas so scrolling
        works even when the pointer is over a thumbnail (not just empty space)."""
        widget.bind("<MouseWheel>", self._on_mousewheel)
        widget.bind("<Button-4>", self._on_mousewheel)
        widget.bind("<Button-5>", self._on_mousewheel)

    # ------------------------------------------------------------- filhoved
    # Hovedet viser filnavn + oprettelsesdato og (hvis relevant) en haengelaas.
    # Datoen har FORTRINSRET: teksten ombrydes til hoejst to linjer, og er der
    # stadig ikke plads, forkortes FILNAVNET -- datoen vises altid.
    def _header_font(self):
        if getattr(self, "_hdr_font", None) is None:
            # Cachet paa viewet, ikke pr. hoved: en tkfont.Font pr. hoved ville
            # lave et nyt Tk-fontobjekt for hver eneste synlig fil.
            self._hdr_font = tkfont.Font(font=theme.FONTS["strong"])
        return self._hdr_font

    @staticmethod
    def _fmt_date(iso: str) -> str:
        """"YYYY-MM-DD" -> "25/12-2025". Tom streng hvis datoen mangler/er skæv."""
        if not iso:
            return ""
        try:
            d = datetime.date.fromisoformat(iso)
        except (ValueError, TypeError):
            return ""
        # Ingen f-string i _() (projektregel); %-formatering med navngivne felter.
        return _("%(day)02d/%(month)02d-%(year)04d") % {
            "day": d.day, "month": d.month, "year": d.year}

    def _fit_prefix(self, text: str, avail: int, font) -> int:
        """Antal tegn af ``text`` der kan staa paa ``avail`` px. Binaersoegning,
        saa det er ~log n maalinger og ikke een pr. tegn."""
        if font.measure(text) <= avail:
            return len(text)
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.measure(text[:mid]) <= avail:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def _ellipsize(self, text: str, avail: int, font) -> str:
        if font.measure(text) <= avail:
            return text
        n = self._fit_prefix(text, max(0, avail - font.measure("…")), font)
        return (text[:n] + "…") if n else "…"

    def _header_text(self, entry, avail: int) -> str:
        """Navn + dato paa hoejst to linjer, med datoen bevaret for enhver pris."""
        font = self._header_font()
        name = Path(entry.path).name
        date = self._fmt_date(entry.creation_date)
        tail = ("   " + date) if date else ""
        avail = max(20, int(avail))
        if font.measure(name + tail) <= avail:
            return name + tail
        cut = self._fit_prefix(name, avail, font)
        if cut <= 0:
            return date or name
        rest = name[cut:]
        room = avail - font.measure(tail)
        if room <= 0:
            # Ekstremt smalt vindue: dropper resten af navnet, men ikke datoen.
            return name[:cut] + "\n" + (date or "")
        return name[:cut] + "\n" + self._ellipsize(rest, room, font) + tail

    def _lock_icon_for(self, entry):
        """(ikonnavn, farve) for filens krypteringstilstand, eller None."""
        key = getattr(entry, "enc_key", "")
        if key == pdf_utils.ENC_ENCRYPTED:
            return "lock", theme.C["warning"]
        if key == pdf_utils.ENC_DECRYPTED:
            return "unlock", theme.C["text_muted"]
        return None

    def _make_header(self, cell):
        entry = cell["entry"]
        selected = (entry.iid == self._selected_file)
        fill = _SELECT_FILL if selected else _HEADER_BG
        ring = _SELECT_RING if selected else _HEADER_BG
        # Raa tk.Frame (ikke ttk): sv-ttk er et pixmap-tema, saa en ttk-widget kan
        # ikke faa markerings-baggrund. Samme moenster som _make_tile.
        holder = tk.Frame(self.canvas, bd=0, highlightthickness=2,
                          highlightbackground=ring, highlightcolor=ring,
                          background=fill)
        widgets = [holder]

        icon_w = 0
        lock = self._lock_icon_for(entry)
        if lock is not None:
            name, color = lock
            img = self.app.icon_factory.get(name, theme.ICON["small"], color)
            lbl = tk.Label(holder, image=img, background=fill)
            lbl.image = img                       # hold referencen i live
            lbl.pack(side="left", padx=(6, 4))
            icon_w = theme.ICON["small"] + 10
            widgets.append(lbl)

        avail = max(40, cell["w"] - icon_w - 4 * theme.SPACE["sm"])
        text = tk.Label(holder, background=fill, foreground=theme.C["text"],
                        font=theme.FONTS["strong"], justify="left", anchor="w",
                        text=self._header_text(entry, avail))
        text.pack(side="left", padx=(0 if icon_w else 6, 4))
        widgets.append(text)

        for w in widgets:
            w.bind("<Button-1>", lambda e, i=entry.iid: self._select_file(i))
            w.bind("<Button-3>", lambda e, i=entry.iid: self._on_header_right_click(e, i))
            self._bind_wheel(w)
        return {"holder": holder}

    def refresh_header(self, iid):
        """Gentegn een fils hoved (navn/dato/haengelaas kan have aendret sig).

        Hovedet er virtualiseret, saa der er kun noget at goere hvis cellen er
        placeret lige nu; ellers tegnes den korrekt naeste gang den ruller ind."""
        key = "h:" + iid
        a = self.active.get(key)
        if a is None:
            return
        cell = a["cell"]
        self._recycle(key)
        self._place(cell)

    def _on_header_right_click(self, event, iid):
        self._select_file(iid)
        self._ctx_file = iid
        entry = self.app.model.entry_by_iid(iid)
        locked = entry is not None and entry.enc_key == pdf_utils.ENC_ENCRYPTED
        # Poster refereres ved numerisk index, aldrig ved oversat label.
        self._file_menu.entryconfig(self._file_ctx_unlock,
                                    state="normal" if locked else "disabled")
        try:
            self._file_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._file_menu.grab_release()

    def _unlock_file_ctx(self):
        if self._ctx_file:
            self.app.unlock_file(self._ctx_file)

    def _delete_file_ctx(self):
        if self._ctx_file:
            self.app.delete_file(self._ctx_file)

    def _select_all_in_file_ctx(self):
        """Marker alle sider i filen (praktisk foran roter/slet/traek)."""
        entry = self.app.model.entry_by_iid(self._ctx_file) if self._ctx_file else None
        if entry is None or not entry.pages:
            return
        uids = [p.uid for p in entry.pages]
        old = set(self._selected)
        self._selected = [u for u in self.page_order if u in set(uids)]
        self._anchor_uid = self._selected[0] if self._selected else None
        self._selected_file = None
        self._repaint_selection(old)
        self._report_selection()

    def _make_tile(self, cell):
        page = cell["page"]
        selected = page.uid in self._selected
        fill = _SELECT_FILL if selected else _TILE_BG
        ring = _SELECT_RING if selected else _TILE_RING
        # 2px kant via highlightthickness (fast create_window-stoerrelse => ingen
        # layout-forskydning ved valg). Ringen skifter blot farve.
        holder = tk.Frame(self.canvas, bd=0, highlightthickness=2,
                          highlightbackground=ring, highlightcolor=ring, background=fill)
        pad = tk.Frame(holder, background=fill)
        pad.place(relx=0.5, y=TILE_PAD, anchor="n", width=TILE_IMG[0], height=TILE_IMG[1])
        img_lbl = tk.Label(pad, background=_TILE_BG)
        img_lbl.pack(fill="both", expand=True)
        num_lbl = tk.Label(holder, background=fill, text=cell.get("num", ""),
                           font=("Segoe UI", 8))
        num_lbl.place(relx=0.5, rely=1.0, y=-3, anchor="s")
        for w in (holder, pad, img_lbl, num_lbl):
            w.bind("<Button-1>", lambda e, u=page.uid: self._select(u))
            w.bind("<Control-Button-1>",
                   lambda e, u=page.uid: (self._select(u, additive=True), "break")[1])
            w.bind("<Shift-Button-1>",
                   lambda e, u=page.uid: (self._select(u, extend=True), "break")[1])
            w.bind("<Button-3>", lambda e, u=page.uid: self._on_tile_right_click(e, u))
            w.bind("<ButtonPress-1>", lambda e, u=page.uid: self._on_tile_press(e, u), add="+")
            w.bind("<B1-Motion>", self._on_tile_motion, add="+")
            w.bind("<ButtonRelease-1>", self._on_tile_release, add="+")
            self._bind_wheel(w)
        return {"holder": holder, "img": img_lbl}

    def _make_locked(self, cell):
        holder = tk.Frame(self.canvas, bd=0, highlightthickness=2,
                          highlightbackground=_TILE_RING, highlightcolor=_TILE_RING,
                          background=_TILE_BG)
        lock_img = self.app.icons.get('lock')
        lbl = tk.Label(holder, background=_TILE_BG, image=lock_img,
                       text=("" if lock_img else "🔒"), compound="center")
        lbl.place(relx=0.5, y=TILE_PAD, anchor="n", width=TILE_IMG[0], height=TILE_IMG[1])
        tk.Label(holder, background=_TILE_BG, font=("Segoe UI", 8),
                 text=_("Låst")).place(relx=0.5, rely=1.0, y=-3, anchor="s")
        self._bind_wheel(holder)
        for ch in holder.winfo_children():
            self._bind_wheel(ch)
        return {"holder": holder}

    # -------------------------------------------------------------- rendering
    @staticmethod
    def _tile_key(page):
        """Render-cachens noegle for en sides flise."""
        return page_render.cache_key(page.src_path, page.src_index, page.rotation,
                                     TILE_IMG, page.crop)

    def _set_tile_image(self, key, pil):
        a = self.active.get(key)
        if a is None or pil is None:
            return
        img = a["w"].get("img")
        if img is None:
            return
        try:
            photo = ImageTk.PhotoImage(pil)
        except Exception:
            return
        a["photo"] = photo          # keep a ref alive
        cell = a.get("cell") or {}
        if cell.get("kind") == "tile":
            # Husk HVILKEN noegle billedet kom fra, saa _request_visible_renders kan
            # se at flisen er forældet efter en rotation/beskaering.
            a["photo_key"] = self._tile_key(cell["page"])
        img.configure(image=photo)

    def _on_tile_ready(self, uid, pil):
        self._set_tile_image(uid, pil)

    # -------------------------------------------------------------- scrolling
    def _on_scrollbar(self, *args):
        self.render_mgr.notify_activity()      # pause prefetch so scrolling is smooth
        self.canvas.yview(*args)
        self._refresh_visible()

    def _on_mousewheel(self, event):
        self.render_mgr.notify_activity()      # pause prefetch so scrolling is smooth
        delta = 0
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        elif event.delta:
            delta = -1 if event.delta > 0 else 1
        # Scroll by roughly one tile-row per wheel notch for a natural feel.
        self.canvas.yview_scroll(delta * 3, "units")
        self._refresh_visible()

    def _on_canvas_configure(self, event):
        if self._layout_after:
            self.after_cancel(self._layout_after)
        self._layout_after = self.after(80, self._relayout)

    def _relayout(self):
        self._layout_after = None
        self._compute_layout()
        self._refresh_visible()

    # -------------------------------------------------------------- navigation
    def focus_page(self):
        """Give the tile canvas keyboard focus and select a page if none is."""
        self.canvas.focus_set()
        if not self._selected and not self._selected_file and self.page_order:
            self._select_and_reveal(self.page_order[0])

    def _page_step(self):
        rows = max(1, self.canvas.winfo_height() // CELL_H)
        return max(1, self.cols * rows)

    def _navigate(self, step):
        if not self.page_order:
            return "break"
        self.render_mgr.notify_activity()
        cur = self._selected_uid
        if cur in self.page_order:
            i = self.page_order.index(cur)
            i = max(0, min(len(self.page_order) - 1, i + step))
        else:
            i = 0 if step >= 0 else len(self.page_order) - 1
        self._select_and_reveal(self.page_order[i])
        return "break"          # don't also let the canvas scroll on arrow keys

    def _select_and_reveal(self, uid):
        self._ensure_visible(uid)      # scroll first so the tile widget exists
        self._select(uid)

    def _ensure_visible(self, uid):
        cell = next((c for c in self.cells if c["key"] == uid), None)
        if not cell:
            return
        top = self.canvas.canvasy(0)
        h = self.canvas.winfo_height()
        denom = max(1, self.total_height)
        if cell["y"] < top:
            self.canvas.yview_moveto(max(0, cell["y"] - PAD) / denom)
        elif cell["y"] + cell["h"] > top + h:
            self.canvas.yview_moveto(max(0, cell["y"] + cell["h"] - h + PAD) / denom)
        self._refresh_visible()

    # -------------------------------------------------------------- selection
    # Markeringen er en ORDNET liste af side-uids. Filmarkering (et klik paa et
    # filhoved) og sidemarkering udelukker hinanden, saa kommandobarens knapper
    # entydigt ved om de arbejder paa sider eller paa hele filer.
    def selected_uids(self) -> list:
        """Markerede sider i modelraekkefoelge (tom liste hvis en fil er valgt)."""
        order = {u: i for i, u in enumerate(self.page_order)}
        return sorted((u for u in self._selected if u in order), key=order.get)

    @property
    def _selected_uid(self):
        """Bagudkompatibel enkelt-uid (main_app laeser den ved "Indsaet side")."""
        sel = self.selected_uids()
        return sel[-1] if sel else None

    @_selected_uid.setter
    def _selected_uid(self, uid):
        self._selected = [uid] if uid else []
        self._anchor_uid = uid

    def _select(self, uid, *, additive=False, extend=False):
        """Marker en side. ``additive`` = Ctrl-klik, ``extend`` = Shift-klik."""
        self.canvas.focus_set()
        old = set(self._selected)
        if extend and self._anchor_uid in self.page_order and uid in self.page_order:
            i, j = self.page_order.index(self._anchor_uid), self.page_order.index(uid)
            lo, hi = min(i, j), max(i, j)
            self._selected = list(self.page_order[lo:hi + 1])
        elif additive:
            sel = set(self._selected)
            sel.discard(uid) if uid in sel else sel.add(uid)
            self._selected = [u for u in self.page_order if u in sel]
            self._anchor_uid = uid
        else:
            self._selected = [uid]
            self._anchor_uid = uid
        self._selected_file = None
        self._repaint_selection(old)
        if not extend and not additive:
            self.pcanvas.goto_page(uid)
        self._report_selection()

    def _select_file(self, iid):
        """Marker en HEL fil (klik paa filhovedet). Rydder sidemarkeringen."""
        self.canvas.focus_set()
        old = set(self._selected)
        self._selected = []
        self._anchor_uid = None
        self._selected_file = iid
        self._repaint_selection(old)
        self._report_selection()

    def _clear_selection(self):
        old = set(self._selected)
        self._selected, self._anchor_uid, self._selected_file = [], None, None
        self._repaint_selection(old)

    def _repaint_selection(self, previously=()):
        """Gentegn kun de fliser/hoveder hvis tilstand faktisk aendrede sig."""
        now = set(self._selected)
        for key in set(previously) | now:
            a = self.active.get(key)
            if a and a["cell"]["kind"] == "tile":
                self._paint_tile_selected(a, key in now)
        for cell in self.cells:
            if cell["kind"] != "header":
                continue
            a = self.active.get(cell["key"])
            if a is not None:
                self._paint_header_selected(a, cell["entry"].iid == self._selected_file)

    def _report_selection(self):
        try:
            if self._selected_file:
                entry = self.app.model.entry_by_iid(self._selected_file)
                if entry is not None:
                    self.app.set_status(_("Fil valgt: %s") % Path(entry.path).name)
                return
            sel = self.selected_uids()
            if len(sel) > 1:
                self.app.set_status(_("%(s)d sider valgt") % {"s": len(sel)})
            elif sel:
                idx = self.page_order.index(sel[0]) + 1
                self.app.set_status(_("Side %(i)d af %(n)d")
                                    % {"i": idx, "n": len(self.page_order)})
            else:
                self.app.set_status()
        except (ValueError, AttributeError, tk.TclError):
            pass

    def _highlight_page(self, uid):
        """Kaldes naar den kontinuerlige fremviser scroller til en ny side.

        Den maa IKKE flytte markeringen: ``goto_page`` -> ``_scroll_to_y`` klamper
        ved dokumentets slutning, saa ``current_page_uid()`` kan returnere en
        SENERE side end den man netop pilede hen til -- og markeringen hoppede
        derfor foran. Her opdateres kun statuslinjen."""
        try:
            idx = self.page_order.index(uid) + 1
        except ValueError:
            return
        if self._selected or self._selected_file:
            return
        self.app.set_status(_("Side %(i)d af %(n)d")
                            % {"i": idx, "n": len(self.page_order)})

    def _paint_header_selected(self, active, selected):
        holder = active["w"].get("holder")
        if holder is None:
            return
        fill = _SELECT_FILL if selected else _HEADER_BG
        ring = _SELECT_RING if selected else _HEADER_BG
        try:
            holder.configure(background=fill, highlightbackground=ring,
                             highlightcolor=ring)
            for child in holder.winfo_children():
                try:
                    child.configure(background=fill)
                except tk.TclError:
                    pass
        except tk.TclError:
            pass

    def _paint_tile_selected(self, active, selected):
        fill = _SELECT_FILL if selected else _TILE_BG
        ring = _SELECT_RING if selected else _TILE_RING
        holder = active["w"]["holder"]
        try:
            holder.configure(background=fill, highlightbackground=ring, highlightcolor=ring)
            # holder-boern er pad + sidenummer (billed-label ligger inde i pad og
            # daekkes af selve billedet, saa dens neutrale baggrund er ligegyldig).
            for child in holder.winfo_children():
                child.configure(background=fill)
        except tk.TclError:
            pass

    # -------------------------------------------------------- Fase 4: editing
    def _on_tile_right_click(self, event, uid):
        # Et hoejreklik inde i en eksisterende markering bevarer den (som i
        # Stifinder); ellers markerer det den flise man ramte.
        if uid not in self._selected:
            self._select(uid)
        self._ctx_uid = uid
        try:
            self._menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._menu.grab_release()

    def _delete_selected_key(self):
        sel = self.selected_uids()
        if sel:
            self.delete_pages(sel)
        elif self._selected_file:
            self.app.delete_file(self._selected_file)
        return "break"

    def _ctx_targets(self) -> list:
        """Hvilke sider en kontekstmenu-handling gaelder.

        Klikkede man inde i en flermarkering, gaelder den hele markeringen;
        ellers kun den flise man ramte."""
        sel = self.selected_uids()
        if self._ctx_uid and self._ctx_uid in sel:
            return sel
        if self._ctx_uid:
            return [self._ctx_uid]
        return sel

    def _rotate_ctx(self, delta):
        uids = self._ctx_targets()
        if uids:
            self.rotate_pages(uids, delta)

    def _delete_ctx(self):
        uids = self._ctx_targets()
        if uids:
            self.delete_pages(uids)

    def _export_ctx(self, fmt):
        uid = self._ctx_uid or self._selected_uid
        if uid:
            self.app.export_single_page(uid, fmt)

    def rotate_pages(self, uids, delta):
        """Roter sider i modellen som EEN undo-handling og opdater fliserne straks.

        Rotation er ikke en render-invalidering: den cachede miniature roteres paa
        stedet (sub-ms) og gemmes under den nye noegle, saa der er nul
        PDF_LOCK-trafik. Side-objekterne muteres in place, saa enhver senere
        rendering bruger allerede den nye rotation.
        """
        uids = [u for u in uids if self.app.model.page_by_uid(u)]
        if not uids:
            return
        pages = {u: self.app.model.page_by_uid(u)[1] for u in uids}
        cached = {u: self.render_mgr.cache.get(self._tile_key(p))
                  for u, p in pages.items()}
        self.app.undo_stack.push(em.rotate_pages_cmd(self.app.model, uids, delta))
        for uid in uids:
            self._retile_after_rotate(uid, pages[uid], cached.get(uid), delta)
        # Vis den nye rotation i den kontinuerlige fremviser (ny layout).
        self.pcanvas.set_document()

    def _retile_after_rotate(self, uid, page, cached, delta):
        """Genbrug den allerede renderede miniature i stedet for at rendere igen."""
        if cached is not None:
            try:
                rotated = cached.rotate(-delta, expand=True)
                rotated.thumbnail(TILE_IMG, Image.Resampling.LANCZOS)
                self.render_mgr.cache.put(self._tile_key(page), rotated)
                self._set_tile_image(uid, rotated)
                return
            except Exception:
                pass
        self._rerequest_tile(uid)

    def _rerequest_tile(self, uid):
        a = self.active.get(uid)
        if not a or a["cell"]["kind"] != "tile":
            return
        a.pop("photo", None)
        cell = a["cell"]
        page = cell["page"]
        a.pop("photo_key", None)
        self.render_mgr.request(
            page.uid, page.src_path, self.app._get_all_passwords(),
            page.src_index, page.rotation, TILE_IMG, self._on_tile_ready,
            priority=1, generation=self.render_mgr._current_generation(),
            crop=page.crop)

    def delete_pages(self, uids):
        """Haard fjernelse fra modellen (kildefilerne roeres ikke) som EEN
        undo-handling. Toemmes en fil helt, fjernes hele FileEntry'en."""
        uids = [u for u in uids if self.app.model.page_by_uid(u)]
        if not uids:
            return
        # Efter sletningen markeres naboen: foerste overlevende EFTER den sidst
        # slettede side, ellers den sidste overlevende foer den.
        order = self.page_order
        doomed = set(uids)
        last = max((i for i, u in enumerate(order) if u in doomed), default=-1)
        nxt = next((u for u in order[last + 1:] if u not in doomed), None)
        if nxt is None:
            nxt = next((u for u in reversed(order[:last]) if u not in doomed), None)
        self.app.undo_stack.push(em.delete_pages_cmd(self.app.model, uids))
        self._selected = [nxt] if nxt else []
        self._anchor_uid = nxt
        self.rebuild()
        if self._selected:
            self._ensure_visible(self._selected[0])
            self.pcanvas.goto_page(self._selected[0])
        self.app.after_model_change()

    # ------------------------------------------------------- traek og slip
    # Moenstret er laant fra den gamle Treeview, men tre ting kunne IKKE porteres:
    #   1. Museevents paa en flise er FLISE-relative -- de skal regnes om til
    #      laerreds-koordinater via canvasx/canvasy.
    #   2. En tk.Frame.place()'et paa et laerred positioneres i WIDGET-koordinater,
    #      ikke laerreds-koordinater, og laerredet ruller under den. Treeview'ens
    #      bbox var allerede viewport-relativ, derfor virkede den gamle kode.
    #   3. Treeview'en auto-scrollede gratis under et traek. Uden det kan man ikke
    #      traekke en side hen til en fil der ligger uden for skaermen.
    DRAG_THRESHOLD = 5          # px foer et klik bliver til et traek
    AUTOSCROLL_ZONE = 30        # px i top/bund der ruller
    AUTOSCROLL_MS = 60

    def _canvas_xy(self, event):
        """Museevent (uanset hvilken widget den kom fra) -> laerreds-koordinater."""
        return (self.canvas.canvasx(event.x_root - self.canvas.winfo_rootx()),
                self.canvas.canvasy(event.y_root - self.canvas.winfo_rooty()))

    def _on_tile_press(self, event, uid):
        self._drag = {"uid": uid, "x": event.x_root, "y": event.y_root, "armed": False}

    def _on_tile_motion(self, event):
        d = self._drag
        if not d:
            return
        if not d["armed"]:
            if (abs(event.x_root - d["x"]) < self.DRAG_THRESHOLD
                    and abs(event.y_root - d["y"]) < self.DRAG_THRESHOLD):
                return
            # Traekker man en flise der ikke var markeret, traekkes netop den.
            if d["uid"] not in self._selected:
                self._select(d["uid"])
            d["armed"] = True
            d["uids"] = self.selected_uids()
            self._make_drag_ghost(len(d["uids"]))
        if self._drag_ghost is not None:
            self._drag_ghost.geometry("+%d+%d" % (event.x_root + 18, event.y_root + 12))
        cx, cy = self._canvas_xy(event)
        d["target"] = self._drop_target_at(cx, cy)
        self._show_drop_indicator(d["target"])
        self._autoscroll(event)

    def _on_tile_release(self, event):
        d, self._drag = self._drag, None
        self._stop_autoscroll()
        self._hide_drop_indicator()
        self._destroy_drag_ghost()
        if not d or not d.get("armed"):
            return
        target = d.get("target")
        uids = d.get("uids") or []
        if not target or not uids:
            return
        kind = target[0]
        if kind == "into":
            _k, dst_iid, visual = target
            entry = self.app.model.entry_by_iid(dst_iid)
            if entry is None:
                return
            pos = self._drop_position(entry, uids, visual)
            # Slippes udsnittet praecis der hvor det allerede ligger, er der intet
            # at fortryde -- undgaa et tomt undo-trin.
            want = set(uids)
            cur = [pg.uid for pg in entry.pages]
            rest = [u for u in cur if u not in want]
            if rest[:pos] + uids + rest[pos:] == cur:
                return
            self.app.undo_stack.push(
                em.move_pages_cmd(self.app.model, uids, dst_iid, pos))
        else:
            self.app.undo_stack.push(
                em.extract_pages_cmd(self.app.model, uids, at_index=target[1]))
        self.rebuild()
        self.reselect(uids)
        self.app.after_model_change()

    @staticmethod
    def _drop_position(entry, uids, visual):
        """Visuel indsaetningsplads -> plads EFTER at de trukne sider er taget ud.

        ``_drop_target_at`` regner i den liste brugeren SER, hvor de trukne sider
        stadig er med. ``move_pages`` indsaetter derimod i listen efter at de er
        fjernet. Traekker man fremad inde i samme fil, skal pladsen derfor
        reduceres med antallet af trukne sider der laa foer den -- ellers lander
        siden i slutningen af filen i stedet for der hvor den blev sluppet.
        """
        want = set(uids)
        before = sum(1 for pg in entry.pages[:visual] if pg.uid in want)
        return max(0, visual - before)

    def cancel_drag(self, event=None):
        """Afbryd et igangvaerende traek (Escape, eller nedrivning af UI)."""
        self._drag = None
        self._stop_autoscroll()
        self._hide_drop_indicator()
        self._destroy_drag_ghost()

    # --- drop-maal -------------------------------------------------------
    def _drop_target_at(self, cx, cy):
        """-> ("into", file_iid, position) | ("extract", file_index) | None."""
        for cell in self.cells:
            if not (cell["x"] <= cx <= cell["x"] + cell["w"]
                    and cell["y"] <= cy <= cell["y"] + cell["h"]):
                continue
            entry = cell["entry"]
            if cell["kind"] == "header":
                return ("into", entry.iid, 0)
            if cell["kind"] == "locked":
                return None
            page = cell["page"]
            try:
                pos = entry.pages.index(page)
            except ValueError:
                return None
            # Venstre halvdel = foer flisen, hoejre halvdel = efter.
            if cx > cell["x"] + cell["w"] / 2:
                pos += 1
            return ("into", entry.iid, pos)
        # Ikke over en celle: mellemrummet mellem to filgrupper betyder "riv ud
        # som egen fil". GROUP_GAP er derfor bevidst bred nok til at kunne rammes.
        return ("extract", self._file_index_at(cy))

    def _file_index_at(self, cy):
        """Hvilket fil-indeks et punkt mellem grupperne svarer til."""
        idx = len(self.app.model.files)
        for cell in self.cells:
            if cell["kind"] != "header":
                continue
            if cy < cell["y"]:
                idx = self.app.model.index_of_iid(cell["entry"].iid)
                break
        return max(0, idx)

    def drop_file_index(self, x_root, y_root):
        """Fil-indeks for et Explorer-drop paa de givne skaermkoordinater.

        Returnerer None hvis punktet ikke er over gitteret (kaldestedet bruger da
        "end"). Retter samtidig at drops i sidevisningen foer altid landede
        nederst, fordi den gamle hit-test kun kendte traeraekker."""
        try:
            cx = self.canvas.canvasx(x_root - self.canvas.winfo_rootx())
            cy = self.canvas.canvasy(y_root - self.canvas.winfo_rooty())
        except tk.TclError:
            return None
        if not (0 <= x_root - self.canvas.winfo_rootx() <= self.canvas.winfo_width()):
            return None
        target = self._drop_target_at(cx, cy)
        if target is None:
            return None
        if target[0] == "extract":
            return target[1]
        idx = self.app.model.index_of_iid(target[1])
        return None if idx < 0 else idx

    # --- visuelle hjaelpere ----------------------------------------------
    def _drop_bar(self):
        if getattr(self, "_dropbar", None) is None or not self._dropbar.winfo_exists():
            self._dropbar = tk.Frame(self.canvas, background=_SELECT_RING)
        return self._dropbar

    def _show_drop_indicator(self, target):
        """Vis hvor siden lander.

        Bemaerk: et canvas-item duer IKKE her. Tk tegner altid indlejrede vinduer
        (``create_window``) oeverst, saa en streg mellem fliserne laa under dem og
        var usynlig. Indikatoren er derfor en rigtig widget, placeret i
        VIEWPORT-koordinater og loeftet op over fliserne.
        """
        if not target:
            self._hide_drop_indicator()
            return
        geom = (self._extract_bar_geometry(target[1]) if target[0] == "extract"
                else self._into_bar_geometry(target))
        if geom is None:
            self._hide_drop_indicator()
            return
        cx, cy, w, h = geom
        x = cx - self.canvas.canvasx(0)
        y = cy - self.canvas.canvasy(0)
        bar = self._drop_bar()
        try:
            bar.place(x=int(x), y=int(y), width=int(w), height=int(h))
            bar.lift()
        except tk.TclError:
            pass
        if target[0] == "extract":
            self.app.set_status(_("Slip for at gøre siden til sin egen fil"))

    def _into_bar_geometry(self, target):
        """Lodret bjaelke praecis paa den plads siden indsaettes."""
        _k, iid, visual = target
        tiles = [c for c in self.cells
                 if c["kind"] == "tile" and c["entry"].iid == iid]
        if not tiles:
            hdr = next((c for c in self.cells
                        if c["kind"] == "header" and c["entry"].iid == iid), None)
            return None if hdr is None else (hdr["x"], hdr["y"], 3, hdr["h"])
        if visual < len(tiles):
            cell = tiles[visual]
            x = cell["x"]
        else:
            cell = tiles[-1]
            x = cell["x"] + cell["w"] - 3
        return (x, cell["y"] + 4, 3, cell["h"] - 8)

    def _extract_band_y(self, file_index):
        """Y-koordinaten paa udtraeks-baandet foran den givne fil (eller efter
        den sidste, naar man slipper under alt indhold)."""
        for c in self.cells:
            if c["kind"] != "header":
                continue
            if self.app.model.index_of_iid(c["entry"].iid) == file_index:
                return max(2, c["y"] - GROUP_GAP // 2)
        return max(2, self.total_height - GROUP_GAP // 2)

    def _extract_bar_geometry(self, file_index):
        """Vandret bjaelke i mellemrummet: her bliver siden sin egen fil."""
        y = self._extract_band_y(file_index)
        width = max(40, self.canvas.winfo_width() - 2 * PAD)
        return (PAD, y - 1, width, 3)

    def _hide_drop_indicator(self):
        bar = getattr(self, "_dropbar", None)
        if bar is not None:
            try:
                bar.place_forget()
            except tk.TclError:
                pass

    def _make_drag_ghost(self, count):
        self._destroy_drag_ghost()
        try:
            g = tk.Toplevel(self)
            g.overrideredirect(True)
            g.attributes("-topmost", True)
            g.attributes("-alpha", 0.7)
            box = tk.Frame(g, background=theme.C["surface"], highlightthickness=1,
                           highlightbackground=theme.C["border_strong"])
            box.pack()
            uid = self._selected[0] if self._selected else None
            found = self.app.model.page_by_uid(uid) if uid else None
            pil = None
            if found is not None:
                pil = self.render_mgr.cache.get(self._tile_key(found[1]))
            if pil is not None:
                photo = ImageTk.PhotoImage(pil)
                lbl = tk.Label(box, image=photo, background=theme.C["surface"])
                # Referencen SKAL leve paa viewet; en lokal ville blive
                # garbage-collected og billedet ville forsvinde.
                self._ghost_photo = photo
                lbl.pack()
            else:
                tk.Label(box, text=_("%(n)d sider") % {"n": count}, padx=10, pady=8,
                         background=theme.C["surface"],
                         foreground=theme.C["text"]).pack()
            if count > 1:
                tk.Label(box, text="+%d" % (count - 1),
                         background=theme.C["danger"],
                         foreground=theme.C["selection_fg"],
                         font=theme.FONTS["small"], padx=3).place(relx=1.0, rely=0.0,
                                                                  anchor="ne")
            self._drag_ghost = g
        except tk.TclError:
            self._drag_ghost = None

    def _destroy_drag_ghost(self):
        g, self._drag_ghost = getattr(self, "_drag_ghost", None), None
        self._ghost_photo = None
        if g is not None:
            try:
                g.destroy()
            except tk.TclError:
                pass

    # --- auto-scroll under traek -----------------------------------------
    def _autoscroll(self, event):
        y = event.y_root - self.canvas.winfo_rooty()
        h = self.canvas.winfo_height()
        direction = 0
        if y < self.AUTOSCROLL_ZONE:
            direction = -1
        elif y > h - self.AUTOSCROLL_ZONE:
            direction = 1
        if direction == 0:
            self._stop_autoscroll()
            return
        if self._autoscroll_after is None:
            self._autoscroll_step(direction)

    def _autoscroll_step(self, direction):
        self.canvas.yview_scroll(direction, "units")
        self._refresh_visible()
        if self._drag and self._drag.get("target"):
            self._show_drop_indicator(self._drag["target"])
        self._autoscroll_after = self.after(
            self.AUTOSCROLL_MS, lambda: self._autoscroll_step(direction))

    def _stop_autoscroll(self):
        if getattr(self, "_autoscroll_after", None) is not None:
            try:
                self.after_cancel(self._autoscroll_after)
            except (tk.TclError, ValueError):
                pass
            self._autoscroll_after = None

    # --- smaa hjaelpere main_app kalder ----------------------------------
    def reselect(self, uids):
        """Genskab en markering efter en rebuild (uids overlever kommandoerne)."""
        live = set(self.page_order)
        keep = [u for u in uids if u in live]
        if not keep:
            return
        old = set(self._selected)
        self._selected = [u for u in self.page_order if u in set(keep)]
        self._anchor_uid = self._selected[0]
        self._selected_file = None
        self._repaint_selection(old)
        self._ensure_visible(self._selected[0])
        self._report_selection()

    def activate_crop_tool(self):
        """Kommandobarens Beskær: slaa beskaerings-vaerktoejet til i fremviseren."""
        self._tool_var.set("crop")
        self.active_tool = "crop"
        self.pcanvas.set_tool("crop")
        self._refresh_tool_icons()
        self.app.set_status(_("Træk en ramme på siden for at beskære den"))

    # ---------------------------------------------------- annotation toolbar
    # Vaerktoejsgrupper: (tool_id, ikonnavn, tooltip). Adskilt af separatorer.
    def _build_anno_toolbar(self, parent):
        """Vaerktoejslinje til sidevisningens annotations-/maskeringsvaerktoejer +
        zoom. Grupperet med smaa overskrifter (Zoom · Vælg · Marker · Fremhæv ·
        Masker) adskilt af separatorer. Rene ikonknapper med hover-tooltip; kun
        det valgte vaerktoejs ikon farves (accent, eller roedt for maskering).
        Driver PageCanvas.
        """
        f = self.app.icon_factory
        bar = ttk.Frame(parent)
        bar.pack(side="top", fill="x", pady=(0, 4))
        self._anno_bar = bar
        self._zoom_var = tk.StringVar(value="100%")
        self._tool_buttons: dict[str, ttk.Radiobutton] = {}
        self._tool_icons: dict[str, str] = {}   # tool_id -> ikonnavn

        def group(title):
            """En gruppe: lille centreret overskrift over en raekke kontroller."""
            g = ttk.Frame(bar)
            g.pack(side="left", padx=(0, 2))
            ttk.Label(g, text=title, font=theme.FONTS["caption"],
                      foreground=theme.C["text_muted"], anchor="center"
                      ).pack(side="top", fill="x")
            row = ttk.Frame(g)
            row.pack(side="top")
            return row

        def sep():
            ttk.Separator(bar, orient="vertical").pack(side="left", fill="y",
                                                       padx=4, pady=(2, 0))

        def tool_btn(row, tool, iconname, label):
            self._tool_icons[tool] = iconname
            btn = ttk.Radiobutton(
                row, value=tool, variable=self._tool_var,
                style="Compact.Toolbutton", command=self._on_tool_change,
                image=f.tool(iconname, self._tool_color(tool, False)))
            btn.pack(side="left", padx=1)
            self._tool_buttons[tool] = btn
            Tooltip.attach(btn, _(label))

        # --- Zoom (som i en PDF-fremviser) ---
        zr = group(_("Zoom"))
        icons_vector.icon_button(
            zr, icon="zoom_out", tip=_("Zoom ud"), command=self._zoom_out,
            factory=f, size=theme.ICON["tool"], style="Compact.Toolbutton"
            ).pack(side="left", padx=(0, 1))
        ze = ttk.Entry(zr, textvariable=self._zoom_var, width=6, justify="center")
        ze.pack(side="left")
        ze.bind("<Return>", lambda e: self._apply_zoom_entry())
        ze.bind("<FocusOut>", lambda e: self._apply_zoom_entry())
        icons_vector.icon_button(
            zr, icon="zoom_in", tip=_("Zoom ind"), command=self._zoom_in,
            factory=f, size=theme.ICON["tool"], style="Compact.Toolbutton"
            ).pack(side="left", padx=(1, 0))
        sep()

        # --- Vælg (navigation/markering) ---
        vr = group(_("Vælg"))
        tool_btn(vr, "hand", "tool_hand", "Flyt")
        tool_btn(vr, "select", "tool_select", "Marker")
        sep()

        # --- Marker (tegnevaerktoejer) + farve. Farven hoerer til her, saa det er
        #     tydeligt at den gaelder tegningerne (og den valgte figur). ---
        mr = group(_("Marker"))
        tool_btn(mr, "rect", "tool_rect", "Boks")
        tool_btn(mr, "circle", "tool_circle", "Cirkel")
        tool_btn(mr, "line", "tool_line", "Streg")
        tool_btn(mr, "ink", "tool_ink", "Frihånd")
        tool_btn(mr, "freetext", "tool_freetext", "Tekst")
        self._color_btn = ttk.Button(
            mr, style="Compact.Toolbutton", command=self._pick_color,
            image=f.swatch_button(self._anno_color_hex(), theme.ICON["tool"]))
        self._color_btn.pack(side="left", padx=(4, 1))
        Tooltip.attach(self._color_btn, _("Vælg farve"))
        sep()

        # --- Fremhæv (tekstmarkering) ---
        hr = group(_("Fremhæv"))
        tool_btn(hr, "highlight", "tool_highlight", "Fremhæv")
        tool_btn(hr, "underline", "tool_underline", "Understreg")
        tool_btn(hr, "strikeout", "tool_strike", "Gennemstreg")
        sep()

        # --- Masker (redaction) + soegning ---
        kr = group(_("Masker"))
        tool_btn(kr, "redact", "tool_redact", "Masker boks")
        tool_btn(kr, "redact_text", "tool_redact_text", "Masker ord")
        se = ttk.Entry(kr, textvariable=self._search_var, width=14)
        se.pack(side="left", padx=(4, 2))
        se.bind("<Return>", lambda e: self._run_search_redaction())
        icons_vector.icon_button(
            kr, icon="search_redact", tip=_("Søg og masker"),
            command=self._run_search_redaction, factory=f,
            size=theme.ICON["tool"], color=theme.C["danger"],
            style="Compact.Toolbutton").pack(side="left", padx=2)
        self._search_status = ttk.Label(kr, text="",
                                        foreground=theme.C["text_muted"])
        self._search_status.pack(side="left", padx=4)

        self._refresh_tool_icons()

    def _tool_color(self, tool: str, selected: bool) -> str:
        """Ikonfarve for et vaerktoej. Maskeringsvaerktoejer er altid roede
        (destruktivt); oevrige er neutrale i hvile og accent naar valgt."""
        if tool in _REDACT_TOOLS:
            return theme.C["danger"]
        return theme.C["accent"] if selected else theme.C["text"]

    def _refresh_tool_icons(self):
        """Gentegn hvert vaerktoejs ikon i den rette farve (ttk nedtoner ikke
        image= via style, saa den valgte-tilstand skal males eksplicit)."""
        f = self.app.icon_factory
        active = self._tool_var.get()
        for tool, btn in self._tool_buttons.items():
            try:
                btn.configure(image=f.tool(self._tool_icons[tool],
                                           self._tool_color(tool, tool == active)))
            except tk.TclError:
                pass

    def _on_tool_change(self):
        self.active_tool = self._tool_var.get()
        self.pcanvas.set_tool(self.active_tool)
        self._refresh_tool_icons()

    def _anno_color_hex(self):
        return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in self._anno_color)

    def _pick_color(self):
        rgb, _hexv = colorchooser.askcolor(color=self._anno_color_hex(),
                                           parent=self, title=_("Vælg farve"))
        if rgb:
            self._anno_color = tuple(c / 255.0 for c in rgb)
            # Farven bruges til (a) NÆSTE nye tegning og (b) den aktuelt valgte
            # annotation, hvis en er valgt -- så farveknappen "virker" både for
            # kommende og allerede tegnede figurer.
            self.pcanvas.set_color(self._anno_color)
            self.pcanvas.recolor_selection(self._anno_color)
            try:
                self._color_btn.configure(image=self.app.icon_factory.swatch_button(
                    self._anno_color_hex(), theme.ICON["tool"]))
            except tk.TclError:
                pass

    def rotate_selected(self, delta):
        """Kommandobarens roter-knapper.

        Gaar gennem :meth:`rotate_pages`, IKKE gennem PageCanvas: den gamle vej
        (``pcanvas.rotate_current``) genopbyggede kun den store fremviser, saa
        miniaturen beholdt sit gamle billede -- og roterede oven i koebet den side
        der laa oeverst i fremviseren frem for den markerede flise."""
        uids = self.selected_uids()
        if not uids:
            return
        self.rotate_pages(uids, delta)

    # --- zoom controls ----------------------------------------------------
    def _on_zoom_change(self, pct):
        try:
            self._zoom_var.set("%d%%" % pct)
        except tk.TclError:
            pass

    def _zoom_in(self):
        self.pcanvas.zoom_in()

    def _zoom_out(self):
        self.pcanvas.zoom_out()

    def _apply_zoom_entry(self):
        val = self._zoom_var.get().strip().rstrip("%").strip()
        self.pcanvas.set_zoom_percent(val)

    # --- search redaction -------------------------------------------------
    def _run_search_redaction(self):
        """Soeg og masker paa tvaers af **alle** aabne filer.

        Grupperingen sker efter ``page.src_path`` -- ikke ``entry.path`` -- fordi en
        fils sideliste kan indeholde sider fra en anden PDF ("Indsaet side"). Hver
        kildefil aabnes dermed praecis en gang, og kun de sider der faktisk ligger i
        modellen scannes."""
        query = self._search_var.get().strip()
        if not query:
            return
        jobs = {}
        for entry in self.app.model.files:
            for p in entry.pages:
                if em.kind_for_path(p.src_path) != em.KIND_PDF:
                    continue
                _path, idxs = jobs.setdefault(os.path.normcase(p.src_path),
                                              (p.src_path, set()))
                idxs.add(p.src_index)
        if not jobs:
            self._set_search_status(_("Ingen forekomster"))
            return
        pw = self.app._get_all_passwords()
        self._set_search_status(_("Søger…"))
        work = [(path, sorted(idxs)) for path, idxs in jobs.values()]
        threading.Thread(target=self._search_worker, args=(work, pw, query),
                         daemon=True).start()

    def _search_worker(self, jobs, pw, query):
        results = {}
        for path, indices in jobs:
            hits = redaction.scan_indices(path, pw, query, indices)
            if hits:
                results[os.path.normcase(path)] = hits
        self.app._queue.put((self._apply_search_redaction, (query, results)))

    def _apply_search_redaction(self, query, results):
        items = []
        if results:
            for entry in self.app.model.files:
                for p in entry.pages:
                    rects = results.get(os.path.normcase(p.src_path), {}).get(p.src_index)
                    if not rects:
                        continue
                    spec = em.AnnotationSpec(kind=an.ANNOT_REDACT, rects=tuple(rects),
                                             color=(0, 0, 0), fill=(0, 0, 0),
                                             source="search", label=query)
                    items.append((p.uid, spec))
        if not items:
            self._set_search_status(_("Ingen forekomster"))
            return
        self.app.undo_stack.push(em.add_annotations_batch_cmd(self.app.model, items))
        self.pcanvas._draw_overlays()
        total = sum(len(spec.rects) for _uid, spec in items)
        self._set_search_status(_("%s forekomster markeret") % total)

    def _set_search_status(self, text):
        try:
            self._search_status.configure(text=text)
        except tk.TclError:
            pass
