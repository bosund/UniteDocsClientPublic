"""Continuous, zoomable multi-page preview (Acrobat-style) for the page view.

Replaces the old single-page preview. Pages are stacked vertically on one scrolling
canvas (a right-hand scrollbar is the "elevator"); the mouse wheel scrolls, Ctrl+wheel
zooms, and the cursor tool pans by dragging. Rendering is virtualised: only pages in
(or near) the viewport are rasterised, at the current zoom, through the shared
:class:`~app.page_render.PageRenderManager` (workers never touch Tk).

Annotations are drawn/edited directly on this canvas. Each visible page owns a
:class:`~app.view_transform.ViewTransform` placed at its absolute canvas position, so
canvas items scroll for free and geometry stays in unrotated source-page points.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk, simpledialog
import dataclasses
import uuid

import pymupdf
from PIL import ImageTk

from . import edit_model as em
from . import pdf_renderer
from . import page_render
from . import annotations as an
from . import redaction
from . import theme
from .view_transform import ViewTransform
from .logging_config import get_logger

logger = get_logger(__name__)

PAGE_GAP = 16
MARGIN = 18
ZOOM_MIN = 0.15
ZOOM_MAX = 6.0
ZOOM_STEPS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)
# Alle farver fra theme.C -- ingen hex i denne fil.
_CANVAS_BG = theme.C["page_gutter"]
_PAGE_SHADOW = theme.C["page_shadow"]
_SEL_OUTLINE = theme.C["accent"]
_PAPER = theme.C["paper"]
_EMPTY_FG = theme.C["page_gutter_fg"]
_CROP_LINE = theme.C["accent"]            # gummibaand under beskaering
_REDACT = theme.C["redact_bar"]
_TEXTSEL = theme.C["selection"]

_TEXT_TOOLS = ("highlight", "underline", "strikeout", "redact_text")
_SHAPE_TOOLS = ("rect", "circle", "line", "ink", "freetext", "redact")


def _lw(v) -> int:
    """Stregbredde som **heltal** til canvas-item-optionen ``-width``.

    Appen saetter LC_NUMERIC til dansk (komma-decimal). Tk's ``-width`` parses
    gennem den locale-afhaengige C-``strtod``, som forkaster strengen "2.0"
    ("bad screen distance") -- mens canvas-KOORDINATER gaar gennem Tcl's
    locale-uafhaengige parser og derfor er fine. Et heltal undgaar problemet helt.
    Uden dette fejler overlay-tegningen tavst (fanget i _draw_overlays), saa en
    tegnet figur aldrig ses i preview.
    """
    try:
        return max(1, int(round(float(v))))
    except (TypeError, ValueError):
        return 1


class PageCanvas(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.rmgr = app.page_render_mgr

        self.zoom = 1.0
        self._suppress_notify = False    # saet mens VI selv scroller (goto_page)
        self.active_tool = "hand"       # "hand" (pan) | "select" | drawing tools
        self.color = (1.0, 0.0, 0.0)
        self.width = 2.0
        self.on_zoom_change = None       # callback(percent:int) set by PageView
        self.on_page_change = None       # callback(uid) when the current page changes
        self._last_page = None

        self.pages = []                  # [{entry,page,uid,x,y,w,h,vt}]
        self.by_uid = {}
        self.geom = {}                   # uid -> (kind, r0, dw, dh)
        self.words = {}                  # uid -> get_text("words")
        self.total_w = 0
        self.total_h = 0
        self._gen = 0
        self._placed = {}                # uid -> True (image drawn at current gen)
        self._photos = {}                # uid -> PhotoImage (keep refs alive)
        self._layout_after = None
        self._render_after = None

        self._draw = None                # in-progress gesture
        self._sel = None                 # (page_uid, annot_uid) selected element
        self._text_selection = None      # {"text","rects","page_uid"}

        self.canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0,
                                background=_CANVAS_BG)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self._yview)
        self.hsb = ttk.Scrollbar(self, orient="horizontal", command=self._xview)
        self.canvas.configure(yscrollcommand=self.vsb.set, xscrollcommand=self.hsb.set)
        self.vsb.grid(row=0, column=1, sticky="ns")
        self.hsb.grid(row=1, column=0, sticky="ew")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        c = self.canvas
        c.configure(takefocus=True)
        c.bind("<Configure>", self._on_configure)
        c.bind("<MouseWheel>", self._on_wheel)
        c.bind("<Button-4>", self._on_wheel)
        c.bind("<Button-5>", self._on_wheel)
        c.bind("<Control-MouseWheel>", self._on_ctrl_wheel)
        c.bind("<Control-Button-4>", self._on_ctrl_wheel)
        c.bind("<Control-Button-5>", self._on_ctrl_wheel)
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_motion)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<Double-Button-1>", self._on_double)
        # Keyboard: arrows scroll the view; PgUp/PgDn change page; Home/End ends.
        c.bind("<Down>", lambda e: (c.yview_scroll(2, "units"), self._after_scroll()))
        c.bind("<Up>", lambda e: (c.yview_scroll(-2, "units"), self._after_scroll()))
        c.bind("<Left>", lambda e: c.xview_scroll(-2, "units"))
        c.bind("<Right>", lambda e: c.xview_scroll(2, "units"))
        c.bind("<Next>", lambda e: self._page_step(1))
        c.bind("<Prior>", lambda e: self._page_step(-1))
        c.bind("<Home>", lambda e: self._goto_index(0))
        c.bind("<End>", lambda e: self._goto_index(len(self.pages) - 1))
        c.bind("<Delete>", lambda e: self.delete_selected())
        c.bind("<Button-3>", self._on_context)
        c.bind("<Control-c>", lambda e: self.copy_selected_text())
        c.bind("<Control-C>", lambda e: self.copy_selected_text())

    # ------------------------------------------------------------------ public
    def focus(self):
        self.canvas.focus_set()

    def set_tool(self, tool):
        self.active_tool = tool
        self._clear_selection()

    def set_color(self, rgb):
        self.color = rgb

    def recolor_selection(self, rgb) -> bool:
        """Giv den aktuelt valgte annotation den nye farve (undo-bar).

        Maskeringer springes over -- de er altid sorte. Returnerer True hvis en
        annotation faktisk blev omfarvet, saa kalderen ved om farvevalget ramte
        et element eller blot skal gaelde naeste nye tegning.
        """
        if not self._sel:
            return False
        page_uid, annot_uid = self._sel
        found = self.app.model.page_by_uid(page_uid)
        if not found:
            return False
        _entry, page = found
        spec = next((a for a in page.annots if a.uid == annot_uid), None)
        if spec is None or spec.kind == an.ANNOT_REDACT:
            return False
        new_color = tuple(rgb)
        if tuple(spec.color) == new_color:
            return True
        # Frisk uid, saa remove(old)/add(new) i do/undo ikke kolliderer.
        new_spec = dataclasses.replace(spec, color=new_color, uid=uuid.uuid4().hex)

        def do():
            self.app.model.remove_annotation(page_uid, annot_uid)
            self.app.model.add_annotation(page_uid, new_spec)

        def undo():
            self.app.model.remove_annotation(page_uid, new_spec.uid)
            self.app.model.add_annotation(page_uid, spec)
        from .undo_stack import Command
        self.app.undo_stack.push(Command("Skift farve", do, undo))
        self._sel = (page_uid, new_spec.uid)
        self._draw_overlays()
        return True

    def zoom_in(self):
        self._set_zoom(self._next_zoom(1))

    def zoom_out(self):
        self._set_zoom(self._next_zoom(-1))

    def set_zoom_percent(self, pct):
        try:
            z = max(ZOOM_MIN, min(ZOOM_MAX, float(pct) / 100.0))
        except (TypeError, ValueError):
            return
        self._set_zoom(z)

    def zoom_percent(self):
        return int(round(self.zoom * 100))

    def current_page_uid(self):
        """Page whose top is nearest the viewport top (for toolbar rotate)."""
        if not self.pages:
            return None
        top = self.canvas.canvasy(0)
        best = self.pages[0]
        for p in self.pages:
            if p["y"] <= top + 40:
                best = p
            else:
                break
        return best["uid"]

    def delete_selected(self):
        if not self._sel:
            return "break"
        page_uid, annot_uid = self._sel
        found = self.app.model.page_by_uid(page_uid)
        if found:
            _, page = found
            spec = next((a for a in page.annots if a.uid == annot_uid), None)
            if spec is not None:
                self.app.undo_stack.push(
                    em.remove_annotation_cmd(self.app.model, page_uid, spec))
        self._clear_selection()
        self._draw_overlays()
        return "break"

    # --------------------------------------------------------------- build/layout
    def set_document(self):
        """(Re)load geometry for every page in the model, then lay out + render."""
        self._gen += 1
        gen = self._gen
        pairs = self.app.model.flatten()
        # Snapshot for the worker (paths + indices), grouped by nothing fancy.
        # src_path pr. SIDE (ikke f.path): indsatte sider peger på deres egen
        # genererede PDF. Kind udledes derfor også af sidens sti.
        snap = [(f.iid, p.uid, em.kind_for_path(p.src_path), p.src_path, p.src_index)
                for (f, p) in pairs]
        pw = self.app._get_all_passwords()
        threading.Thread(target=self._geometry_worker, args=(gen, snap, pw),
                         daemon=True).start()

    # keep old name used by PageView
    def rebuild(self):
        self.set_document()

    def _geometry_worker(self, gen, snap, pw):
        geom = {}
        for iid, uid, kind, path, idx in snap:
            if kind == em.KIND_PDF:
                g = pdf_renderer.page_geometry(path, pw, idx)
                if g:
                    r0, dw, dh = g
                    geom[uid] = ("pdf", r0, dw, dh)
                else:
                    geom[uid] = ("pdf", 0, 595.0, 842.0)
            else:
                try:
                    from PIL import Image
                    with Image.open(path) as im:
                        w, h = im.size
                    geom[uid] = ("image", 0, float(w), float(h))
                except Exception:
                    geom[uid] = ("image", 0, 595.0, 842.0)
        self.app._queue.put((self._on_geometry_ready, (gen, geom)))

    def _on_geometry_ready(self, gen, geom):
        if gen != self._gen:
            return
        self.geom = geom
        # Default zoom: fit the first page's width to the viewport, once.
        if not self.pages and self.app.model.files:
            self._fit_width_pending = True
        self._compute_layout()
        self._refresh_visible()

    def _compute_layout(self):
        self.pages = []
        self.by_uid = {}
        cw = max(1, self.canvas.winfo_width())
        pairs = self.app.model.flatten()

        # optional one-time fit-to-width using the first page
        if getattr(self, "_fit_width_pending", False) and pairs and self.geom:
            first_uid = pairs[0][1].uid
            g = self.geom.get(first_uid)
            if g:
                # NB: ikke '_' som kastevariabel -- det binder gettext-'_' som
                # funktions-lokal og faar tom-tilstandens _("...") nedenfor til at
                # kaste UnboundLocalError.
                _kind0, r0, dw, dh = g
                shown_w = dh if (r0 % 180 == 90) else dw
                if shown_w > 0:
                    self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, (cw - 2 * MARGIN) / shown_w))
            self._fit_width_pending = False
            if self.on_zoom_change:
                self.on_zoom_change(self.zoom_percent())

        y = MARGIN
        maxw = cw
        for entry, page in pairs:
            g = self.geom.get(page.uid, ("pdf", 0, 595.0, 842.0))
            _kind, r0, dw, dh = g
            # unrotated dims
            if r0 % 180 == 90:
                uw, uh = dh, dw
            else:
                uw, uh = dw, dh
            total = (r0 + page.rotation) % 360
            # Er siden beskaaret, er det cropens maal der vises -- ikke sidens.
            crop = page.crop
            if crop:
                cw_pts = max(1.0, float(crop[2]) - float(crop[0]))
                ch_pts = max(1.0, float(crop[3]) - float(crop[1]))
            else:
                cw_pts, ch_pts = uw, uh
            if total % 180 == 90:
                shown_w_pts, shown_h_pts = ch_pts, cw_pts
            else:
                shown_w_pts, shown_h_pts = cw_pts, ch_pts
            w = max(1, int(round(shown_w_pts * self.zoom)))
            h = max(1, int(round(shown_h_pts * self.zoom)))
            x = max(MARGIN, (cw - w) // 2)
            vt = ViewTransform(uw, uh, total, self.zoom, x, y, crop=crop)
            rec = {"entry": entry, "page": page, "uid": page.uid,
                   "x": x, "y": y, "w": w, "h": h, "vt": vt, "kind": _kind}
            self.pages.append(rec)
            self.by_uid[page.uid] = rec
            maxw = max(maxw, w + 2 * MARGIN)
            y += h + PAGE_GAP
        self.total_w = maxw
        self.total_h = y + MARGIN
        self.canvas.configure(scrollregion=(0, 0, self.total_w, self.total_h))
        # keep placed set but force re-place at new geometry/zoom
        self._placed = {}
        self._photos = {}
        self.canvas.delete("all")
        if not self.pages:
            self.canvas.create_text(MARGIN, MARGIN, anchor="nw", fill=_EMPTY_FG,
                                    text=_("Ingen sider at vise. Tilføj filer."))

    # --------------------------------------------------------------- rendering
    def _visible_span(self, buffer=None):
        h = self.canvas.winfo_height()
        if buffer is None:
            buffer = h
        top = self.canvas.canvasy(0) - buffer
        bottom = self.canvas.canvasy(0) + h + buffer
        return top, bottom

    def _refresh_visible(self):
        self._render_after = None
        if not self.pages:
            return
        top, bottom = self._visible_span()
        pw = self.app._get_all_passwords()
        gen = self.rmgr._current_generation()
        for rec in self.pages:
            if rec["y"] + rec["h"] < top or rec["y"] > bottom:
                continue
            if rec["uid"] in self._placed:
                continue
            self._placed[rec["uid"]] = True
            page = rec["page"]
            box = (rec["w"], rec["h"])
            cached = self.rmgr.cache.get(page_render.cache_key(
                page.src_path, page.src_index, page.rotation, box, page.crop))
            if cached is not None:
                self._set_page_image(rec["uid"], cached)
            else:
                # page frame placeholder while it renders
                self.canvas.create_rectangle(
                    rec["x"], rec["y"], rec["x"] + rec["w"], rec["y"] + rec["h"],
                    fill=_PAPER, outline=_PAGE_SHADOW, tags=("pageframe", "p:" + rec["uid"]))
                self.rmgr.request(rec["uid"], page.src_path, pw, page.src_index,
                                  page.rotation, box, self._on_page_ready,
                                  priority=1, generation=gen, crop=page.crop)
        self._draw_overlays()
        self._notify_page()

    def _notify_page(self):
        if getattr(self, "_suppress_notify", False):
            return
        cur = self.current_page_uid()
        if cur is not None and cur != self._last_page:
            self._last_page = cur
            if self.on_page_change:
                self.on_page_change(cur)

    def _on_page_ready(self, uid, pil):
        if uid not in self.by_uid or pil is None:
            return
        self._set_page_image(uid, pil)
        self._draw_overlays()

    def _set_page_image(self, uid, pil):
        rec = self.by_uid.get(uid)
        if rec is None:
            return
        try:
            photo = ImageTk.PhotoImage(pil)
        except Exception:
            return
        self._photos[uid] = photo
        self.canvas.delete("p:" + uid)
        self.canvas.create_image(rec["x"], rec["y"], anchor="nw", image=photo,
                                 tags=("pageimg", "p:" + uid))

    # --------------------------------------------------------------- overlays
    def _page_words(self, rec):
        uid = rec["uid"]
        if uid not in self.words:
            self.words[uid] = pdf_renderer.page_words(
                rec["page"].src_path, self.app._get_all_passwords(), rec["page"].src_index)
        return self.words[uid]

    def _spec_hex(self, spec):
        return "#%02x%02x%02x" % tuple(
            int(round(max(0.0, min(1.0, c)) * 255)) for c in spec.color)

    def _draw_overlays(self):
        self.canvas.delete("anno")
        self.canvas.delete("selbox")
        top, bottom = self._visible_span(buffer=self.canvas.winfo_height())
        for rec in self.pages:
            if rec["y"] + rec["h"] < top or rec["y"] > bottom:
                continue
            found = self.app.model.page_by_uid(rec["uid"])
            if not found:
                continue
            _, page = found
            for spec in page.annots:
                try:
                    self._draw_spec(rec, spec)
                except Exception as e:
                    logger.debug("overlay %s: %s", spec.kind, e)
        # staaende tekstmarkering (markoer-vaerktoejet)
        self.canvas.delete("selrect")
        sel = self._text_selection
        if sel:
            rec = self.by_uid.get(sel["page_uid"])
            if rec is not None:
                vt = rec["vt"]
                for r in sel["rects"]:
                    x0, y0 = vt.pdf_to_canvas(r[0], r[1])
                    x1, y1 = vt.pdf_to_canvas(r[2], r[3])
                    self.canvas.create_rectangle(min(x0, x1), min(y0, y1),
                                                 max(x0, x1), max(y0, y1),
                                                 fill=_TEXTSEL, stipple="gray50",
                                                 outline="", tags=("selrect",))
        # selection box
        if self._sel:
            bbox = self._annot_bbox(*self._sel)
            if bbox:
                self.canvas.create_rectangle(*bbox, outline=_SEL_OUTLINE, width=2,
                                             dash=(4, 3), tags=("selbox",))

    def _draw_spec(self, rec, spec):
        vt = rec["vt"]
        c = self.canvas
        col = self._spec_hex(spec)
        kind = spec.kind
        tag = ("anno", "a:%s:%s" % (rec["uid"], spec.uid))
        if kind == "redact":
            # En maskering kan daekke FLERE rects (ord-maskering markerer flere
            # ord i én spec). Tegn dem alle -- ellers ser man kun det foerste ord
            # maskeret i preview, selv om gem korrekt masker alle.
            for r in spec.rects:
                cx0, cy0 = vt.pdf_to_canvas(r[0], r[1])
                cx1, cy1 = vt.pdf_to_canvas(r[2], r[3])
                c.create_rectangle(cx0, cy0, cx1, cy1, fill=_REDACT,
                                   outline=_REDACT, stipple="gray50", tags=tag)
        elif kind in ("rect", "freetext"):
            x0, y0, x1, y1 = spec.rects[0]
            cx0, cy0 = vt.pdf_to_canvas(x0, y0)
            cx1, cy1 = vt.pdf_to_canvas(x1, y1)
            c.create_rectangle(cx0, cy0, cx1, cy1, outline=col,
                               width=_lw(spec.width), tags=tag)
            if kind == "freetext" and spec.text:
                c.create_text(min(cx0, cx1) + 3, min(cy0, cy1) + 2, anchor="nw",
                              text=spec.text, fill=col,
                              width=max(10, int(abs(cx1 - cx0)) - 6), tags=tag)
        elif kind == "circle":
            x0, y0, x1, y1 = spec.rects[0]
            cx0, cy0 = vt.pdf_to_canvas(x0, y0)
            cx1, cy1 = vt.pdf_to_canvas(x1, y1)
            c.create_oval(cx0, cy0, cx1, cy1, outline=col, width=_lw(spec.width), tags=tag)
        elif kind == "line":
            x0, y0, x1, y1 = spec.rects[0]
            cx0, cy0 = vt.pdf_to_canvas(x0, y0)
            cx1, cy1 = vt.pdf_to_canvas(x1, y1)
            c.create_line(cx0, cy0, cx1, cy1, fill=col, width=_lw(spec.width), tags=tag)
        elif kind == "ink":
            for stroke in spec.strokes:
                pts = []
                for (x, y) in stroke:
                    pts.extend(vt.pdf_to_canvas(x, y))
                if len(pts) >= 4:
                    c.create_line(*pts, fill=col, width=_lw(spec.width),
                                  smooth=True, tags=tag)
        elif kind in ("highlight", "underline", "strikeout"):
            for r in spec.rects:
                x0, y0, x1, y1 = r
                cx0, cy0 = vt.pdf_to_canvas(x0, y0)
                cx1, cy1 = vt.pdf_to_canvas(x1, y1)
                if kind == "highlight":
                    c.create_rectangle(cx0, cy0, cx1, cy1, outline="", fill=col,
                                       stipple="gray50", tags=tag)
                else:
                    yy = max(cy0, cy1) if kind == "underline" else (cy0 + cy1) / 2
                    c.create_line(cx0, yy, cx1, yy, fill=col, width=2, tags=tag)

    def _annot_bbox(self, page_uid, annot_uid):
        items = self.canvas.find_withtag("a:%s:%s" % (page_uid, annot_uid))
        if not items:
            return None
        x0 = y0 = 1e18
        x1 = y1 = -1e18
        for it in items:
            bx = self.canvas.bbox(it)
            if bx:
                x0 = min(x0, bx[0]); y0 = min(y0, bx[1])
                x1 = max(x1, bx[2]); y1 = max(y1, bx[3])
        if x1 < x0:
            return None
        return (x0 - 3, y0 - 3, x1 + 3, y1 + 3)

    # --------------------------------------------------------------- hit-testing
    def _page_at(self, cx, cy):
        for rec in self.pages:
            if rec["x"] <= cx <= rec["x"] + rec["w"] and rec["y"] <= cy <= rec["y"] + rec["h"]:
                return rec
        return None

    def _annot_at(self, cx, cy):
        """Bbox-based hit test (topmost first) so clicking inside an unfilled
        shape still selects it."""
        for it in reversed(self.canvas.find_withtag("anno")):
            bx = self.canvas.bbox(it)
            if bx and bx[0] - 3 <= cx <= bx[2] + 3 and bx[1] - 3 <= cy <= bx[3] + 3:
                for t in self.canvas.gettags(it):
                    if t.startswith("a:"):
                        _, page_uid, annot_uid = t.split(":", 2)
                        return (page_uid, annot_uid)
        return None

    # --------------------------------------------------------------- mouse
    def _cxy(self, event):
        return self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)

    def _on_press(self, event):
        self.canvas.focus_set()
        cx, cy = self._cxy(event)
        tool = self.active_tool
        if tool == "hand":
            self._clear_selection()
            self.canvas.scan_mark(event.x, event.y)
            self._draw = {"mode": "pan"}
            return
        if tool == "select":
            # Markoer-vaerktoejet: rammer klikket en annotation, vaelges den; ellers
            # begynder en TEKST-markering. Musemarkering af tekst laa i den gamle
            # enkeltside-fremviser og faldt paa gulvet da den kontinuerlige
            # fremviser afloeste den -- kun det ubrugte ``_text_selection``-felt
            # blev tilbage.
            hit = self._annot_at(cx, cy)
            self._clear_text_selection()
            if hit:
                self._sel = hit
                self._draw = None
                self._draw_overlays()
                return
            self._sel = None
            rec = self._page_at(cx, cy)
            if rec is None:
                self._draw = None
                self._draw_overlays()
                return
            self._draw = {"mode": "text", "rec": rec, "x0": cx, "y0": cy, "hits": []}
            return
        rec = self._page_at(cx, cy)
        if rec is None:
            self._draw = None
            return
        if tool in _TEXT_TOOLS:
            self._draw = {"mode": "text", "rec": rec, "x0": cx, "y0": cy, "hits": []}
        else:
            self._draw = {"mode": "shape", "rec": rec, "x0": cx, "y0": cy,
                          "item": None, "pts": [(cx, cy)]}

    def _on_motion(self, event):
        if not self._draw:
            return
        mode = self._draw["mode"]
        if mode == "pan":
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            self._draw_overlays()
            return
        cx, cy = self._cxy(event)
        if mode == "text":
            self._update_text_sel(cx, cy)
            return
        rec = self._draw["rec"]
        col = self._hex(self.color)
        c = self.canvas
        tool = self.active_tool
        if tool == "ink":
            self._draw["pts"].append((cx, cy))
            px, py = self._draw["pts"][-2]
            c.create_line(px, py, cx, cy, fill=col, width=2, tags=("wip",))
            return
        if self._draw["item"] is not None:
            c.delete(self._draw["item"])
        x0, y0 = self._draw["x0"], self._draw["y0"]
        if tool == "line":
            self._draw["item"] = c.create_line(x0, y0, cx, cy, fill=col, width=2, tags=("wip",))
        elif tool == "circle":
            self._draw["item"] = c.create_oval(x0, y0, cx, cy, outline=col, width=2, tags=("wip",))
        elif tool == "crop":
            self._draw["item"] = c.create_rectangle(
                x0, y0, cx, cy, outline=_CROP_LINE, width=2, dash=(4, 3), tags=("wip",))
        else:
            self._draw["item"] = c.create_rectangle(x0, y0, cx, cy, outline=col, width=2, tags=("wip",))

    def _update_text_sel(self, cx, cy):
        rec = self._draw["rec"]
        vt = rec["vt"]
        sel = pymupdf.Rect(*vt.rect_from_canvas(self._draw["x0"], self._draw["y0"], cx, cy))
        hits = []
        self.canvas.delete("selrect")
        for w in self._page_words(rec):
            wr = pymupdf.Rect(w[0], w[1], w[2], w[3])
            if wr.intersects(sel):
                r = (float(w[0]), float(w[1]), float(w[2]), float(w[3]))
                hits.append((r, w[4]))
                cx0, cy0 = vt.pdf_to_canvas(r[0], r[1])
                cx1, cy1 = vt.pdf_to_canvas(r[2], r[3])
                self.canvas.create_rectangle(cx0, cy0, cx1, cy1, outline="",
                                             fill=_TEXTSEL, stipple="gray50",
                                             tags=("selrect", "wip"))
        self._draw["hits"] = hits

    def _on_release(self, event):
        if not self._draw:
            return
        d = self._draw
        self._draw = None
        mode = d["mode"]
        if mode == "pan":
            self._after_scroll()
            return
        cx, cy = self._cxy(event)
        rec = d["rec"]
        vt = rec["vt"]
        tool = self.active_tool
        if mode == "text":
            self.canvas.delete("wip")
            hits = d.get("hits", [])
            rects = [r for r, _w in hits]
            if tool == "select":
                # Markeringen bliver STAAENDE (modsat markup-vaerktoejerne, der
                # forbruger den med det samme), saa hoejreklik kan handle paa den.
                if rects:
                    self._text_selection = {
                        "page_uid": rec["uid"],
                        "rects": tuple(rects),
                        "text": " ".join(w for _r, w in hits),
                    }
                else:
                    self._clear_text_selection()
                self._draw_overlays()
                return
            self.canvas.delete("selrect")
            if not rects:
                return
            if tool in _TEXT_TOOLS:
                self._commit_text(rec["uid"], tool, rects)
            return
        # shape
        self.canvas.delete("wip")
        if tool == "ink":
            pts = d["pts"]
            if len(pts) < 2:
                return
            stroke = tuple(vt.canvas_to_pdf(px, py) for px, py in pts)
            self._commit(rec["uid"], em.AnnotationSpec(kind=an.ANNOT_INK, strokes=(stroke,),
                                                       color=self.color, width=self.width))
        elif tool == "line":
            x0, y0 = vt.canvas_to_pdf(d["x0"], d["y0"])
            x1, y1 = vt.canvas_to_pdf(cx, cy)
            self._commit(rec["uid"], em.AnnotationSpec(kind=an.ANNOT_LINE,
                         rects=((x0, y0, x1, y1),), color=self.color, width=self.width))
        else:
            r = vt.rect_from_canvas(d["x0"], d["y0"], cx, cy)
            if (r[2] - r[0]) < 2 or (r[3] - r[1]) < 2:
                return
            if tool == "crop":
                # Beskaering gaelder KUN den side gestussen skete paa: et
                # rektangel fra en A4-portraetside er meningsloest paa en
                # landskabsside i samme markering.
                self.app.undo_stack.push(
                    em.set_pages_crop_cmd(self.app.model, [(rec["uid"], r)]))
                self.rebuild()
                self.app.after_crop_change(rec["uid"])
                return
            if tool == "rect":
                self._commit(rec["uid"], em.AnnotationSpec(kind=an.ANNOT_RECT, rects=(r,),
                             color=self.color, width=self.width))
            elif tool == "circle":
                self._commit(rec["uid"], em.AnnotationSpec(kind=an.ANNOT_CIRCLE, rects=(r,),
                             color=self.color, width=self.width))
            elif tool == "redact":
                self._commit(rec["uid"], em.AnnotationSpec(kind=an.ANNOT_REDACT, rects=(r,),
                             color=(0, 0, 0), fill=(0, 0, 0), source="manual"))
            elif tool == "freetext":
                # Vis tekstboksen dér hvor rektanglet blev tegnet (oeverste
                # venstre hjoerne), ikke midt paa skaermen.
                at = self._canvas_to_screen(min(d["x0"], cx), min(d["y0"], cy))
                text = self._ask_multiline(_("Tekst"), _("Skriv tekst:"), at=at)
                if text:
                    fitted = self._fit_freetext(r, text, 11.0)
                    self._commit(rec["uid"], em.AnnotationSpec(kind=an.ANNOT_FREETEXT,
                                 rects=(fitted,), text=text, color=self.color, fontsize=11.0))

    def copy_selected_text(self):
        """Laeg den markerede tekst paa udklipsholderen."""
        text = self.selected_text()
        if not text:
            return "break"
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.app.set_status(_("Tekst kopieret"), transient_ms=4000,
                                kind="success")
        except tk.TclError:
            pass
        return "break"

    def _commit_from_selection(self, kind):
        """Lav en annotation ud af den staaende tekstmarkering."""
        sel = self._text_selection
        if not sel or not sel.get("rects"):
            return
        rects = tuple(sel["rects"])
        if kind == "redact":
            spec = em.AnnotationSpec(kind=an.ANNOT_REDACT, rects=rects,
                                     color=(0, 0, 0), fill=(0, 0, 0),
                                     source="manual")
        elif kind == "underline":
            spec = em.AnnotationSpec(kind=an.ANNOT_UNDERLINE, rects=rects,
                                     color=self.color)
        elif kind == "strikeout":
            spec = em.AnnotationSpec(kind=an.ANNOT_STRIKEOUT, rects=rects,
                                     color=self.color)
        else:
            spec = em.AnnotationSpec(kind=an.ANNOT_HIGHLIGHT, rects=rects,
                                     color=self.color)
        page_uid = sel["page_uid"]
        self._clear_text_selection()
        self._commit(page_uid, spec)

    def _on_context(self, event):
        """Hoejreklik i fremviseren. Er der markeret tekst, tilbydes handlinger
        paa den; ellers er der intet at vise."""
        self.canvas.focus_set()
        text = self.selected_text()
        if not text:
            return
        short = (text[:28] + "\u2026") if len(text) > 28 else text
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label=_("Kopiér"), command=self.copy_selected_text)
        menu.add_separator()
        menu.add_command(label=_("Fremhæv markering"),
                         command=lambda: self._commit_from_selection("highlight"))
        menu.add_command(label=_("Understreg markering"),
                         command=lambda: self._commit_from_selection("underline"))
        menu.add_command(label=_("Gennemstreg markering"),
                         command=lambda: self._commit_from_selection("strikeout"))
        menu.add_command(label=_("Masker markering"),
                         command=lambda: self._commit_from_selection("redact"))
        menu.add_separator()
        menu.add_command(label=_("Masker alle forekomster af \"%s\"") % short,
                         command=lambda t=text: self._redact_all_of(t))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _redact_all_of(self, text):
        """Send teksten videre til sidevisningens soege-maskering (alle filer).

        Feltet ryddes bagefter: soegningen er udfoert, og en efterladt streng ser ud
        som om der stadig er noget at soege efter."""
        self._clear_text_selection()
        view = getattr(self.app, "page_view", None)
        if view is None:
            return
        try:
            view._search_var.set(text)
            view._run_search_redaction()
            view._search_var.set("")
        except (AttributeError, tk.TclError):
            pass

    def _on_double(self, event):
        cx, cy = self._cxy(event)
        hit = self._annot_at(cx, cy)
        if not hit:
            return
        page_uid, annot_uid = hit
        found = self.app.model.page_by_uid(page_uid)
        if not found:
            return
        _, page = found
        spec = next((a for a in page.annots if a.uid == annot_uid), None)
        if spec is None or spec.kind != an.ANNOT_FREETEXT:
            return
        new_text = self._ask_multiline(_("Tekst"), _("Skriv tekst:"),
                                       initial=spec.text,
                                       at=(event.x_root, event.y_root))
        if new_text is None or new_text == spec.text:
            return
        # replace: remove old, add edited (kept at same rect, refit height)
        rec = self.by_uid.get(page_uid)
        r = spec.rects[0] if spec.rects else (0, 0, 100, 20)
        fitted = self._fit_freetext(r, new_text, spec.fontsize)
        new_spec = em.AnnotationSpec(kind=an.ANNOT_FREETEXT, rects=(fitted,), text=new_text,
                                     color=spec.color, fontsize=spec.fontsize)

        def do():
            self.app.model.remove_annotation(page_uid, annot_uid)
            self.app.model.add_annotation(page_uid, new_spec)

        def undo():
            self.app.model.remove_annotation(page_uid, new_spec.uid)
            self.app.model.add_annotation(page_uid, spec)
        from .undo_stack import Command
        self.app.undo_stack.push(Command("Rediger tekst", do, undo))
        self._sel = (page_uid, new_spec.uid)
        self._draw_overlays()

    # --------------------------------------------------------------- commits
    def _commit(self, page_uid, spec):
        self.app.undo_stack.push(em.add_annotation_cmd(self.app.model, page_uid, spec))
        self._draw_overlays()

    def _commit_text(self, page_uid, tool, rects):
        if not rects:
            return
        if tool == "redact_text":
            spec = em.AnnotationSpec(kind=an.ANNOT_REDACT, rects=tuple(rects),
                                     color=(0, 0, 0), fill=(0, 0, 0), source="manual")
        else:
            kind = {"highlight": an.ANNOT_HIGHLIGHT, "underline": an.ANNOT_UNDERLINE,
                    "strikeout": an.ANNOT_STRIKEOUT}[tool]
            spec = em.AnnotationSpec(kind=kind, rects=tuple(rects), color=self.color)
        self._commit(page_uid, spec)

    # --------------------------------------------------------------- zoom/scroll
    def _next_zoom(self, direction):
        cur = self.zoom
        if direction > 0:
            for z in ZOOM_STEPS:
                if z > cur + 1e-6:
                    return z
            return min(ZOOM_MAX, cur * 1.25)
        for z in reversed(ZOOM_STEPS):
            if z < cur - 1e-6:
                return z
        return max(ZOOM_MIN, cur / 1.25)

    def _set_zoom(self, z, anchor=None):
        """Zoom omkring et fast punkt -- som standard viewportens CENTRUM.

        Den gamle udgave ankrede kun lodret, og kun til den oeverste sides top,
        og roerte aldrig ``xview``. Ved 300 % er siden bredere end viewporten, og
        naar ``scrollregion``-bredden springer fra ~cw til w+2*MARGIN, peger Tk's
        bevarede x-broek pludselig et helt andet sted hen -- siden forsvandt ud af
        billedet. Nu udtrykkes ankerpunktet RELATIVT til en side, saa det
        overlever den nye layout, og saettes tilbage i centrum paa begge akser.

        ``anchor`` er et (canvas_x, canvas_y)-punkt; udelades det, bruges midten.
        """
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if abs(z - self.zoom) < 1e-6:
            return
        cw = max(1, self.canvas.winfo_width())
        ch = max(1, self.canvas.winfo_height())
        ax, ay = anchor if anchor else (self.canvas.canvasx(cw / 2.0),
                                        self.canvas.canvasy(ch / 2.0))
        rec = self._page_at(ax, ay) or self._nearest_page(ay)
        uid, fx, fy = None, 0.0, 0.0
        if rec is not None:
            uid = rec["uid"]
            # Bevidst IKKE klampet til [0,1]: falder centrum i mellemrummet mellem
            # to sider, beholder det sit forhold til ankersiden, og det er lige
            # praecis det der faar gestussen til at foeles stabil.
            fx = (ax - rec["x"]) / max(1.0, rec["w"])
            fy = (ay - rec["y"]) / max(1.0, rec["h"])

        self.zoom = z
        self._compute_layout()
        self._refresh_visible()

        rec = self.by_uid.get(uid) if uid else None
        if rec is not None:
            nx = rec["x"] + fx * rec["w"]
            ny = rec["y"] + fy * rec["h"]
            self.canvas.xview_moveto(max(0.0, nx - cw / 2.0) / max(1, self.total_w))
            self.canvas.yview_moveto(max(0.0, ny - ch / 2.0) / max(1, self.total_h))
            self._refresh_visible()
        if self.on_zoom_change:
            self.on_zoom_change(self.zoom_percent())

    def _nearest_page(self, cy):
        """Siden hvis midte ligger taettest paa ``cy`` (naar centrum rammer et
        mellemrum mellem to sider)."""
        if not self.pages:
            return None
        return min(self.pages, key=lambda r: abs(r["y"] + r["h"] / 2.0 - cy))

    def _scroll_to_y(self, y):
        denom = max(1, self.total_h)
        self.canvas.yview_moveto(max(0.0, min(1.0, y / denom)))

    def goto_page(self, uid):
        """Scroll fremviseren til en side UDEN at melde et sideskift tilbage.

        Uden daempningen opstod en feedback-loekke: _select -> goto_page ->
        _scroll_to_y -> _refresh_visible -> _notify_page -> on_page_change ->
        tilbage i markeringen. Normalt landede den paa samme uid, men
        ``yview_moveto`` KLAMPER ved sidste skaerm, saa naer dokumentets slutning
        pegede current_page_uid() paa en senere side -- og markeringen hoppede
        foran den man netop pilede hen til."""
        rec = self.by_uid.get(uid)
        if rec is None:
            return
        self._suppress_notify = True
        try:
            self._scroll_to_y(rec["y"] - MARGIN)
            self._refresh_visible()
        finally:
            self._suppress_notify = False
        self._last_page = uid

    def _goto_index(self, i):
        if 0 <= i < len(self.pages):
            self.goto_page(self.pages[i]["uid"])
        return "break"

    def _page_step(self, direction):
        cur = self.current_page_uid()
        idx = next((k for k, p in enumerate(self.pages) if p["uid"] == cur), 0)
        self._goto_index(max(0, min(len(self.pages) - 1, idx + direction)))
        return "break"

    def _yview(self, *args):
        self.canvas.yview(*args)
        self._after_scroll()

    def _xview(self, *args):
        self.canvas.xview(*args)

    def _on_wheel(self, event):
        self.rmgr.notify_activity()
        delta = 0
        if event.num == 4:
            delta = -1
        elif event.num == 5:
            delta = 1
        elif event.delta:
            delta = -1 if event.delta > 0 else 1
        self.canvas.yview_scroll(delta * 3, "units")
        self._after_scroll()
        return "break"

    def _on_ctrl_wheel(self, event):
        up = (event.num == 4) or (getattr(event, "delta", 0) > 0)
        self._set_zoom(self._next_zoom(1 if up else -1))
        return "break"

    def _after_scroll(self):
        if self._render_after:
            self.after_cancel(self._render_after)
        self._render_after = self.after(30, self._refresh_visible)

    def _on_configure(self, event):
        if self._layout_after:
            self.after_cancel(self._layout_after)
        self._layout_after = self.after(120, self._relayout)

    def _relayout(self):
        self._layout_after = None
        if self.pages or self.geom:
            self._compute_layout()
            self._refresh_visible()

    # --------------------------------------------------------------- helpers
    def _clear_text_selection(self):
        self._text_selection = None
        try:
            self.canvas.delete("selrect")
        except tk.TclError:
            pass

    def selected_text(self) -> str:
        sel = self._text_selection
        return (sel or {}).get("text", "").strip()

    def _clear_selection(self):
        self._sel = None
        self.canvas.delete("selbox")

    @staticmethod
    def _hex(rgb):
        return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in rgb)

    def _fit_freetext(self, rect, text, fontsize):
        x0, y0, x1, y1 = rect
        width = max(60.0, x1 - x0)
        cpl = max(1, int(width / (fontsize * 0.52)))
        lines = 0
        for para in text.split("\n"):
            length = 0
            n = 0
            for wd in para.split(" "):
                wlen = len(wd) + (1 if length > 0 else 0)
                if length + wlen > cpl and length > 0:
                    n += 1
                    length = len(wd)
                else:
                    length += wlen
            lines += max(1, n + 1)
        height = max(y1 - y0, lines * fontsize * 1.35 + 8)
        return (x0, y0, x0 + width, y0 + height)

    def _canvas_to_screen(self, canvas_x, canvas_y):
        """Canvas-koordinat -> absolut skaerm-koordinat (til dialog-placering)."""
        x = self.canvas.winfo_rootx() + int(canvas_x - self.canvas.canvasx(0))
        y = self.canvas.winfo_rooty() + int(canvas_y - self.canvas.canvasy(0))
        return x, y

    def _ask_multiline(self, title, prompt, initial="", at=None):
        win = tk.Toplevel(self)
        win.title(title)
        win.transient(self.winfo_toplevel())
        ttk.Label(win, text=prompt).pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(win, width=44, height=6, wrap="word")
        if initial:
            txt.insert("1.0", initial)
        txt.pack(fill="both", expand=True, padx=10)
        txt.focus_set()
        result = {"v": None}
        btns = ttk.Frame(win)
        btns.pack(fill="x", padx=10, pady=8)

        def ok():
            result["v"] = txt.get("1.0", "end-1c")
            win.destroy()

        def cancel():
            win.destroy()
        ttk.Button(btns, text=_("OK"), command=ok).pack(side="right")
        ttk.Button(btns, text=_("Annuller"), command=cancel).pack(side="right", padx=(0, 6))
        win.bind("<Escape>", lambda e: cancel())
        win.bind("<Control-Return>", lambda e: ok())
        # Placér boksen dér hvor brugeren tegnede/klikkede -- ikke midt paa
        # skaermen. Klemmes inden for skaermkanten.
        if at is not None:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
            x = max(4, min(int(at[0]), sw - w - 4))
            y = max(4, min(int(at[1]), sh - h - 4))
            win.geometry("+%d+%d" % (x, y))
        try:
            win.grab_set()
        except tk.TclError:
            pass
        self.wait_window(win)
        v = result["v"]
        return v.strip() if v and v.strip() else None
