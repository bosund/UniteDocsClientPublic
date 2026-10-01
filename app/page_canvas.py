"""Kontinuerlig, zoombar fler-side-fremviser (Acrobat-agtig).

Siderne stables lodret paa ét rullende lærred; musehjulet scroller, Ctrl+hjul
zoomer, og haand-vaerktoejet panorerer ved traek. Rendering er virtualiseret:
kun sider i (eller taet paa) udsnittet rasteres, ved den aktuelle zoom, gennem
den delte :class:`~app.page_render.PageRenderManager` -- hvis workere aldrig
roerer UI'et.

Annotationer tegnes og redigeres direkte her. Hver synlig side ejer en
:class:`~app.view_transform.ViewTransform` placeret paa sin absolutte
laerreds-position, saa geometrien altid udtrykkes i **urroterede
kilde-side-punkter** og rotation forbliver en ren visnings-sag.

**Alt males i ét lag.** 8.x byggede canvas-*items* med tags (``anno``,
``selbox``, ``wip``, ``p:<uid>``) og hit-testede via ``find_withtag``/``bbox``.
Her tegner ``paintEvent`` direkte fra modellen, og hit-test regnes ud af
geometrien. Det fjerner tre klasser af fejl paa én gang: items der overlever
deres side, ``-width``-strenge der brækker paa dansk decimalkomma, og
indlejrede vinduer der altid laa oeverst.
"""

from __future__ import annotations

import dataclasses
import uuid

from PySide6.QtCore import QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QAbstractScrollArea, QApplication, QDialog,
                               QFrame, QMenu, QPlainTextEdit, QPushButton,
                               QVBoxLayout, QLabel)

from .undo_stack import N_
from . import annotations as an
from . import edit_model as em
from . import icons_vector
from . import page_render
from . import pdf_renderer
from . import qt_util
from . import theme
from .localization import LocalizationManager
from .logging_config import get_logger
from .view_transform import ViewTransform

logger = get_logger(__name__)
_ = LocalizationManager.get_text

PAGE_GAP = 16
MARGIN = 18
ZOOM_MIN = 0.15
ZOOM_MAX = 6.0
ZOOM_STEPS = (0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0)

_TEXT_TOOLS = ("highlight", "underline", "strikeout", "redact_text")

#: Kortere traek end saa mange pixels er et klik, ikke en markering. Uden det
#: ville et enkelt klik med fremhaevningsvaerktoejet lave en annotation.
_DRAG_SLOP = 3
_SHAPE_TOOLS = ("rect", "circle", "line", "ink", "freetext", "redact", "crop")


def _qcolor(rgb, alpha: int = 255) -> QColor:
    """(r, g, b) i 0..1 -> ``QColor``. Alfa i 0..255."""
    c = QColor(*[int(round(max(0.0, min(1.0, v)) * 255)) for v in rgb[:3]])
    c.setAlpha(alpha)
    return c


class _MultilineDialog(QDialog):
    """Lille tekstboks til fritekst-annotationer."""

    def __init__(self, parent, title: str, prompt: str, initial: str = ""):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.resize(420, 220)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(theme.SPACE["sm"])
        lay.addWidget(QLabel(prompt, self))
        self._text = QPlainTextEdit(self)
        self._text.setPlainText(initial)
        lay.addWidget(self._text, 1)
        cancel = QPushButton(_("Annuller"), self)
        ok = QPushButton(_("OK"), self)
        ok.setDefault(True)
        cancel.clicked.connect(self.reject)
        ok.clicked.connect(self.accept)
        lay.addWidget(qt_util.button_row(self, cancel, ok))
        self._text.setFocus()

    def value(self) -> str | None:
        v = self._text.toPlainText()
        return v.strip() if v and v.strip() else None


class PageCanvas(QAbstractScrollArea):

    zoom_changed = Signal(int)      # procent
    page_changed = Signal(str)      # uid paa den side der nu er oeverst

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.rmgr = app.page_render_mgr

        self.zoom = 1.0
        self.active_tool = "hand"       # "hand" (pan) | "select" | tegnevaerktoejer
        self.color = (1.0, 0.0, 0.0)
        self.width = 2.0

        self.pages: list[dict] = []      # [{entry, page, uid, x, y, w, h, vt, kind}]
        self.by_uid: dict[str, dict] = {}
        self.geom: dict[str, tuple] = {}
        self.words: dict[str, list] = {}
        self.total_w = 0
        self.total_h = 0
        self._gen = 0
        self._pix: dict[str, QPixmap] = {}
        self._requested: set[str] = set()
        self._fit_width_pending = False
        self._suppress_notify = False    # saettes mens VI selv scroller
        self._last_page: str | None = None

        self._draw = None                # igangvaerende gestus
        self._sel = None                 # (page_uid, annot_uid)
        self._text_selection = None      # {"text", "rects", "page_uid"}

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setSingleStep(40)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.horizontalScrollBar().valueChanged.connect(lambda _v: self.viewport().update())

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._refresh_visible)

        self._layout_timer = QTimer(self)
        self._layout_timer.setSingleShot(True)
        self._layout_timer.timeout.connect(self._relayout)
        self._apply_cursor()

    # ------------------------------------------------------------- offentligt
    def set_tool(self, tool: str) -> None:
        self.active_tool = tool
        self._clear_selection()
        self._apply_cursor()
        self.viewport().update()

    def set_color(self, rgb) -> None:
        self.color = rgb

    def zoom_in(self) -> None:
        self._set_zoom(self._next_zoom(1))

    def zoom_out(self) -> None:
        self._set_zoom(self._next_zoom(-1))

    def set_zoom_percent(self, pct) -> None:
        try:
            self._set_zoom(max(ZOOM_MIN, min(ZOOM_MAX, float(pct) / 100.0)))
        except (TypeError, ValueError):
            return

    def zoom_percent(self) -> int:
        return int(round(self.zoom * 100))

    def redraw_overlays(self) -> None:
        """Bevaret navn fra 8.x. Nu er en gentegning alt der skal til."""
        self.viewport().update()

    def current_page_uid(self) -> str | None:
        """Siden hvis top er naermest udsnittets top."""
        if not self.pages:
            return None
        top = self.verticalScrollBar().value()
        best = self.pages[0]
        for p in self.pages:
            if p["y"] <= top + 40:
                best = p
            else:
                break
        return best["uid"]

    def selected_text(self) -> str:
        return (self._text_selection or {}).get("text", "").strip()

    def copy_selected_text(self) -> None:
        text = self.selected_text()
        if not text:
            return
        QApplication.clipboard().setText(text)
        self.app.set_status(_("Tekst kopieret"), transient_ms=4000, kind="success")

    def delete_selected(self) -> None:
        if not self._sel:
            return
        page_uid, annot_uid = self._sel
        found = self.app.model.page_by_uid(page_uid)
        if found:
            _entry, page = found
            spec = next((a for a in page.annots if a.uid == annot_uid), None)
            if spec is not None:
                self.app.undo_stack.push(
                    em.remove_annotation_cmd(self.app.model, page_uid, spec))
        self._clear_selection()
        self.viewport().update()

    def recolor_selection(self, rgb) -> bool:
        """Giv den aktuelt valgte annotation den nye farve (undo-bar).

        Maskeringer springes over -- de er altid sorte. Returnerer True hvis en
        annotation faktisk blev omfarvet, saa kalderen ved om farvevalget ramte
        et element eller blot skal gaelde naeste nye tegning."""
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
        self._swap_annotation(page_uid, spec, new_spec, N_("Skift farve"))
        return True

    def _swap_annotation(self, page_uid, old_spec, new_spec, label: str) -> None:
        from .undo_stack import Command

        def do():
            self.app.model.remove_annotation(page_uid, old_spec.uid)
            self.app.model.add_annotation(page_uid, new_spec)

        def undo():
            self.app.model.remove_annotation(page_uid, new_spec.uid)
            self.app.model.add_annotation(page_uid, old_spec)

        self.app.undo_stack.push(Command(label, do, undo))
        self._sel = (page_uid, new_spec.uid)
        self.viewport().update()

    # -------------------------------------------------------- dokument/layout
    def set_document(self) -> None:
        """Genindlaes geometrien for hver side i modellen, layout derefter."""
        self._gen += 1
        gen = self._gen
        pairs = self.app.model.flatten()
        # src_path pr. SIDE (ikke entry.path): indsatte sider peger paa deres
        # egen genererede PDF. Kind udledes derfor ogsaa af sidens sti.
        snap = [(f.iid, p.uid, em.kind_for_path(p.src_path), p.src_path, p.src_index)
                for (f, p) in pairs]
        qt_util.run_in_thread(self._geometry_worker, gen, snap,
                              self.app._get_all_passwords(), name="page-geometry")

    def rebuild(self) -> None:
        self.set_document()

    def _geometry_worker(self, gen, snap, pw) -> None:
        geom = {}
        for _iid, uid, kind, path, idx in snap:
            if kind == em.KIND_PDF:
                g = pdf_renderer.page_geometry(path, pw, idx)
                geom[uid] = ("pdf", *g) if g else ("pdf", 0, 595.0, 842.0)
            else:
                try:
                    from PIL import Image
                    with Image.open(path) as im:
                        w, h = im.size
                    geom[uid] = ("image", 0, float(w), float(h))
                except Exception:
                    geom[uid] = ("image", 0, 595.0, 842.0)
        self.app._queue.put((self._on_geometry_ready, (gen, geom)))

    def _on_geometry_ready(self, gen, geom) -> None:
        if gen != self._gen:
            return
        self.geom = geom
        # Standardzoom: tilpas foerste sides bredde til udsnittet, én gang.
        if not self.pages and self.app.model.files:
            self._fit_width_pending = True
        self._compute_layout()
        self._refresh_visible()

    def _compute_layout(self) -> None:
        self.pages = []
        self.by_uid = {}
        cw = max(1, self.viewport().width())
        pairs = self.app.model.flatten()

        if self._fit_width_pending and pairs and self.geom:
            g = self.geom.get(pairs[0][1].uid)
            if g:
                _kind0, r0, dw, dh = g
                shown_w = dh if (r0 % 180 == 90) else dw
                if shown_w > 0:
                    self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, (cw - 2 * MARGIN) / shown_w))
            self._fit_width_pending = False
            self.zoom_changed.emit(self.zoom_percent())

        y = MARGIN
        maxw = cw
        for entry, page in pairs:
            kind, r0, dw, dh = self.geom.get(page.uid, ("pdf", 0, 595.0, 842.0))
            uw, uh = (dh, dw) if r0 % 180 == 90 else (dw, dh)
            total = (r0 + page.rotation) % 360
            # Er siden beskaaret, er det cropens maal der vises -- ikke sidens.
            crop = page.crop
            if crop:
                cw_pts = max(1.0, float(crop[2]) - float(crop[0]))
                ch_pts = max(1.0, float(crop[3]) - float(crop[1]))
            else:
                cw_pts, ch_pts = uw, uh
            shown_w_pts, shown_h_pts = ((ch_pts, cw_pts) if total % 180 == 90
                                        else (cw_pts, ch_pts))
            w = max(1, int(round(shown_w_pts * self.zoom)))
            h = max(1, int(round(shown_h_pts * self.zoom)))
            x = max(MARGIN, (cw - w) // 2)
            rec = {"entry": entry, "page": page, "uid": page.uid,
                   "x": x, "y": y, "w": w, "h": h, "kind": kind,
                   "vt": ViewTransform(uw, uh, total, self.zoom, x, y, crop=crop)}
            self.pages.append(rec)
            self.by_uid[page.uid] = rec
            maxw = max(maxw, w + 2 * MARGIN)
            y += h + PAGE_GAP
        self.total_w = maxw
        self.total_h = y + MARGIN
        # Ved ny zoom/geometri skal alle billeder bestilles igen i den nye stoerrelse.
        self._pix.clear()
        self._requested.clear()
        self._sync_scrollbars()

    def _sync_scrollbars(self) -> None:
        vp = self.viewport()
        vsb, hsb = self.verticalScrollBar(), self.horizontalScrollBar()
        vsb.setPageStep(max(1, vp.height()))
        vsb.setRange(0, max(0, self.total_h - vp.height()))
        hsb.setPageStep(max(1, vp.width()))
        hsb.setRange(0, max(0, self.total_w - vp.width()))

    def resizeEvent(self, event):  # noqa: N802 - Qt-API
        super().resizeEvent(event)
        self._layout_timer.start(120)

    def _relayout(self) -> None:
        if self.pages or self.geom:
            self._compute_layout()
            self._refresh_visible()

    # ---------------------------------------------------------- rendering
    def _origin(self) -> QPoint:
        return QPoint(self.horizontalScrollBar().value(),
                      self.verticalScrollBar().value())

    def _visible_span(self, buffer: int | None = None):
        h = self.viewport().height()
        top = self.verticalScrollBar().value()
        if buffer is None:
            buffer = h
        return top - buffer, top + h + buffer

    def _on_scrolled(self) -> None:
        self.rmgr.notify_activity()
        self.viewport().update()
        self._render_timer.start(30)

    def _refresh_visible(self) -> None:
        if not self.pages:
            self.viewport().update()
            return
        top, bottom = self._visible_span()
        pw = self.app._get_all_passwords()
        gen = self.rmgr._current_generation()
        for rec in self.pages:
            if rec["y"] + rec["h"] < top or rec["y"] > bottom:
                continue
            uid = rec["uid"]
            if uid in self._requested:
                continue
            self._requested.add(uid)
            page = rec["page"]
            box = (rec["w"], rec["h"])
            cached = self.rmgr.cache.get(page_render.cache_key(
                page.src_path, page.src_index, page.rotation, box, page.crop))
            if cached is not None:
                self._pix[uid] = icons_vector.pil_to_qpixmap(cached)
            else:
                self.rmgr.request(uid, page.src_path, pw, page.src_index,
                                  page.rotation, box, self._on_page_ready,
                                  priority=1, generation=gen, crop=page.crop)
        self.viewport().update()
        self._notify_page()

    def _on_page_ready(self, uid, pil) -> None:
        if uid not in self.by_uid or pil is None:
            return
        self._pix[uid] = icons_vector.pil_to_qpixmap(pil)
        self.viewport().update()

    def _notify_page(self) -> None:
        if self._suppress_notify:
            return
        cur = self.current_page_uid()
        if cur is not None and cur != self._last_page:
            self._last_page = cur
            self.page_changed.emit(cur)

    def _page_words(self, rec):
        """Sidens ord. Billeder har intet tekstlag -- de faar kun ord via OCR."""
        uid = rec["uid"]
        if uid not in self.words:
            page = rec["page"]
            self.words[uid] = (
                pdf_renderer.page_words(page.src_path,
                                        self.app._get_all_passwords(),
                                        page.src_index)
                if rec["kind"] == "pdf" else [])
        return self.words[uid]

    # ---------------------------------------------------- ord og tekstlinjer
    # Scannede sider har intet tekstlag. Tekstvaerktoejerne kan derfor ikke ramme
    # noget paa dem, foer brugeren har koert **Tekstgenkendelse** (knappen i
    # vaerktoejslinjen). Fremviseren OCR'er ikke af sig selv: en stille koersel paa
    # "den side man staar paa" gav ingen forklaring paa hvorfor markeringen
    # virkede paa én side og ikke paa den naeste.

    @staticmethod
    def _word_chars(words) -> int:
        return sum(len(w[4].strip()) for w in words)

    def has_text(self, rec) -> bool:
        """Er der ord at markere paa siden?"""
        return self._word_chars(self._page_words(rec)) > 0

    def apply_ocr_words(self, by_uid: dict) -> None:
        """Tag imod ordlister fra en tekstgenkendelse. Kun UI-traaden."""
        for uid, words in by_uid.items():
            if words:
                self.words[uid] = words
        self.viewport().update()

    def _hint_no_text(self) -> None:
        self.app.set_status(
            _("Siden har ingen tekst at markere — kør Tekstgenkendelse først"),
            transient_ms=8000, kind="warning")

    def _visual_lines(self, rec) -> list:
        """Ordene grupperet i de linjer **brugeren ser**, i laeserraekkefoelge.

        Grupperingen sker i canvas-rummet, ikke i A-space: paa en roteret side er
        en tekstlinje kun vandret paa skaermen. To ord hoerer til samme linje naar
        deres lodrette udstraekning overlapper med over halvdelen af den mindste
        hoejde -- et forhold, ikke en fast tolerance, saa OCR'ens ujaevne
        ord-rammer ikke river en linje i stykker.
        """
        vt = rec["vt"]
        items = []
        for w in self._page_words(rec):
            if not w[4].strip():
                continue
            x0, y0 = vt.pdf_to_canvas(w[0], w[1])
            x1, y1 = vt.pdf_to_canvas(w[2], w[3])
            items.append((QRectF(min(x0, x1), min(y0, y1),
                                 max(1.0, abs(x1 - x0)), max(1.0, abs(y1 - y0))), w))
        items.sort(key=lambda it: (it[0].center().y(), it[0].left()))

        lines = []
        for box, w in items:
            cur = lines[-1] if lines else None
            if cur is not None:
                overlap = (min(cur["bottom"], box.bottom())
                           - max(cur["top"], box.top()))
                if overlap > 0.5 * min(box.height(), cur["bottom"] - cur["top"]):
                    cur["words"].append((box, w))
                    cur["top"] = min(cur["top"], box.top())
                    cur["bottom"] = max(cur["bottom"], box.bottom())
                    continue
            lines.append({"top": box.top(), "bottom": box.bottom(),
                          "words": [(box, w)]})
        for line in lines:
            line["words"].sort(key=lambda it: it[0].left())
        return lines

    @staticmethod
    def _locate(lines, x: float, y: float) -> tuple:
        """(linje, ord) taettest paa punktet. Uden for siden klemmes der ind."""
        best_i, best_d = 0, None
        for i, line in enumerate(lines):
            if line["top"] <= y <= line["bottom"]:
                best_i = i
                break
            d = line["top"] - y if y < line["top"] else y - line["bottom"]
            if best_d is None or d < best_d:
                best_i, best_d = i, d
        words = lines[best_i]["words"]
        best_j, best_dx = 0, None
        for j, (box, _w) in enumerate(words):
            if box.left() <= x <= box.right():
                return best_i, j
            dx = box.left() - x if x < box.left() else x - box.right()
            if best_dx is None or dx < best_dx:
                best_j, best_dx = j, dx
        return best_i, best_j

    def _line_runs(self, lines, p0, p1) -> list:
        """Ordene mellem to punkter -- ét sammenhaengende loeb **pr. linje**.

        Det er dét der goer markeringen til en tekstmarkering og ikke en ramme:
        en vandret bevaegelse rammer kun den linje markoeren er paa, og et traek
        ned tager resten af den foerste linje, hele de mellemliggende og
        begyndelsen af den sidste -- som i enhver anden laeser.
        """
        i0, j0 = self._locate(lines, *p0)
        i1, j1 = self._locate(lines, *p1)
        if (i0, j0) > (i1, j1):
            (i0, j0), (i1, j1) = (i1, j1), (i0, j0)
        runs = []
        for i in range(i0, i1 + 1):
            words = lines[i]["words"]
            a = j0 if i == i0 else 0
            b = j1 if i == i1 else len(words) - 1
            run = words[a:b + 1]
            if run:
                runs.append(run)
        return runs

    # ------------------------------------------------------------- maling
    def paintEvent(self, event):  # noqa: N802 - Qt-API
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor(theme.C["page_gutter"]))
        if not self.pages:
            p.setPen(QColor(theme.C["page_gutter_fg"]))
            p.setFont(theme.font("base"))
            p.drawText(QRect(MARGIN, MARGIN, self.viewport().width() - 2 * MARGIN, 40),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       _("Ingen sider at vise. Tilføj filer."))
            p.end()
            return

        o = self._origin()
        p.translate(-o.x(), -o.y())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        top, bottom = self._visible_span(buffer=self.viewport().height())

        for rec in self.pages:
            if rec["y"] + rec["h"] < top or rec["y"] > bottom:
                continue
            r = QRect(rec["x"], rec["y"], rec["w"], rec["h"])
            pm = self._pix.get(rec["uid"])
            if pm is not None and not pm.isNull():
                p.drawPixmap(r, pm)
            else:
                # Hvidt papir med en diskret skygge mens siden renderes.
                p.fillRect(r, QColor(theme.C["paper"]))
                p.setPen(QPen(QColor(theme.C["page_shadow"]), 1))
                p.drawRect(r)
            self._paint_annotations(p, rec)

        self._paint_text_selection(p)
        self._paint_selection_box(p)
        self._paint_wip(p)
        p.end()

    def _paint_annotations(self, p: QPainter, rec) -> None:
        found = self.app.model.page_by_uid(rec["uid"])
        if not found:
            return
        _entry, page = found
        for spec in page.annots:
            try:
                self._paint_spec(p, rec, spec)
            except Exception as e:
                logger.debug("overlay %s: %s", spec.kind, e)

    def _paint_spec(self, p: QPainter, rec, spec) -> None:
        vt = rec["vt"]
        col = _qcolor(spec.color)
        kind = spec.kind
        pen = QPen(col, max(1.0, float(spec.width or 1.0)))
        pen.setCosmetic(True)

        def crect(r) -> QRectF:
            x0, y0 = vt.pdf_to_canvas(r[0], r[1])
            x1, y1 = vt.pdf_to_canvas(r[2], r[3])
            return QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0))

        if kind == "redact":
            # En maskering kan daekke FLERE rects (ord-maskering markerer flere
            # ord i én spec). Tegn dem alle -- ellers ser man kun det foerste ord
            # maskeret i preview, selv om gem korrekt masker alle.
            fill = QColor(theme.C["redact_bar"])
            fill.setAlpha(190)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(fill)
            for r in spec.rects:
                p.drawRect(crect(r))
            p.setBrush(Qt.BrushStyle.NoBrush)
        elif kind in ("rect", "freetext"):
            rr = crect(spec.rects[0])
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(rr)
            if kind == "freetext" and spec.text:
                f = QFont(theme.font("base"))
                f.setPointSizeF(max(6.0, float(spec.fontsize or 11.0) * self.zoom * 0.75))
                p.setFont(f)
                p.drawText(rr.adjusted(3, 2, -3, -2),
                           int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
                           | int(Qt.TextFlag.TextWordWrap), spec.text)
        elif kind == "circle":
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(crect(spec.rects[0]))
        elif kind == "line":
            x0, y0, x1, y1 = spec.rects[0]
            cx0, cy0 = vt.pdf_to_canvas(x0, y0)
            cx1, cy1 = vt.pdf_to_canvas(x1, y1)
            p.setPen(pen)
            p.drawLine(int(cx0), int(cy0), int(cx1), int(cy1))
        elif kind == "ink":
            p.setPen(pen)
            for stroke in spec.strokes:
                pts = [vt.pdf_to_canvas(x, y) for (x, y) in stroke]
                for i in range(1, len(pts)):
                    p.drawLine(int(pts[i - 1][0]), int(pts[i - 1][1]),
                               int(pts[i][0]), int(pts[i][1]))
        elif kind in ("highlight", "underline", "strikeout"):
            for r in spec.rects:
                rr = crect(r)
                if kind == "highlight":
                    # Ægte alfa frem for Tk's "gray50"-stipple: fremhaevningen
                    # ser ud som i en PDF-laeser, og teksten under kan laeses.
                    p.setPen(Qt.PenStyle.NoPen)
                    p.setBrush(_qcolor(spec.color, 90))
                    p.drawRect(rr)
                    p.setBrush(Qt.BrushStyle.NoBrush)
                else:
                    yy = rr.bottom() if kind == "underline" else rr.center().y()
                    p.setPen(QPen(col, 2))
                    p.drawLine(int(rr.left()), int(yy), int(rr.right()), int(yy))

    def _paint_text_selection(self, p: QPainter) -> None:
        sel = self._text_selection
        rects = None
        if self._draw and self._draw.get("mode") == "text":
            rects = [(r, self._draw["rec"]) for r, _w in self._draw.get("hits", [])]
        elif sel:
            rec = self.by_uid.get(sel["page_uid"])
            if rec is not None:
                rects = [(r, rec) for r in sel["rects"]]
        if not rects:
            return
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.C["selection"]).lighter(120))
        p.setOpacity(0.35)
        for r, rec in rects:
            vt = rec["vt"]
            x0, y0 = vt.pdf_to_canvas(r[0], r[1])
            x1, y1 = vt.pdf_to_canvas(r[2], r[3])
            p.drawRect(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))
        p.setOpacity(1.0)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_selection_box(self, p: QPainter) -> None:
        if not self._sel:
            return
        box = self._annot_bbox(*self._sel)
        if box is None:
            return
        pen = QPen(QColor(theme.C["accent"]), 2, Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(box.adjusted(-3, -3, 3, 3))

    def _paint_wip(self, p: QPainter) -> None:
        """Gummibaandet under en igangvaerende tegne-gestus."""
        d = self._draw
        if not d or d.get("mode") != "shape":
            return
        tool = self.active_tool
        x0, y0 = d["x0"], d["y0"]
        x1, y1 = d.get("x1", x0), d.get("y1", y0)
        col = (QColor(theme.C["accent"]) if tool == "crop" else _qcolor(self.color))
        pen = QPen(col, 2)
        if tool == "crop":
            pen.setStyle(Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.BrushStyle.NoBrush)
        if tool == "ink":
            pts = d.get("pts", [])
            for i in range(1, len(pts)):
                p.drawLine(int(pts[i - 1][0]), int(pts[i - 1][1]),
                           int(pts[i][0]), int(pts[i][1]))
        elif tool == "line":
            p.drawLine(int(x0), int(y0), int(x1), int(y1))
        elif tool == "circle":
            p.drawEllipse(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))
        else:
            p.drawRect(QRectF(min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)))

    # ------------------------------------------------------------ hit-test
    def _annot_bbox(self, page_uid, annot_uid) -> QRectF | None:
        """Annotationens omsluttende rektangel i laerreds-koordinater.

        Regnes ud af specen, ikke af tegnede items -- derfor er den korrekt
        allerede foer foerste maling og kan ikke pege paa et forældet item."""
        rec = self.by_uid.get(page_uid)
        if rec is None:
            return None
        found = self.app.model.page_by_uid(page_uid)
        if not found:
            return None
        _entry, page = found
        spec = next((a for a in page.annots if a.uid == annot_uid), None)
        if spec is None:
            return None
        return self._spec_bbox(rec, spec)

    @staticmethod
    def _spec_bbox(rec, spec) -> QRectF | None:
        vt = rec["vt"]
        xs, ys = [], []
        for r in (spec.rects or ()):
            for (px, py) in ((r[0], r[1]), (r[2], r[3])):
                cx, cy = vt.pdf_to_canvas(px, py)
                xs.append(cx)
                ys.append(cy)
        for stroke in (spec.strokes or ()):
            for (px, py) in stroke:
                cx, cy = vt.pdf_to_canvas(px, py)
                xs.append(cx)
                ys.append(cy)
        if not xs:
            return None
        return QRectF(min(xs), min(ys), max(1.0, max(xs) - min(xs)),
                      max(1.0, max(ys) - min(ys)))

    def _page_at(self, cx, cy):
        for rec in self.pages:
            if (rec["x"] <= cx <= rec["x"] + rec["w"]
                    and rec["y"] <= cy <= rec["y"] + rec["h"]):
                return rec
        return None

    def _annot_at(self, cx, cy):
        """Bbox-baseret hit-test (oeverste foerst), saa et klik inde i en
        ufyldt figur stadig vaelger den."""
        for rec in reversed(self.pages):
            found = self.app.model.page_by_uid(rec["uid"])
            if not found:
                continue
            _entry, page = found
            for spec in reversed(page.annots):
                box = self._spec_bbox(rec, spec)
                if box is not None and box.adjusted(-3, -3, 3, 3).contains(cx, cy):
                    return (rec["uid"], spec.uid)
        return None

    # --------------------------------------------------------------- mus
    def _cxy(self, event):
        o = self._origin()
        pos = event.position().toPoint()
        return pos.x() + o.x(), pos.y() + o.y()

    def _apply_cursor(self) -> None:
        # Tekstvaerktoejerne (fremhaev/understreg/gennemstreg/masker ord) markerer
        # ORD, ikke et frit areal, saa de skal have samme I-bjaelke som markoeren.
        # Krydset lovede en frihaandsramme og var derfor misvisende.
        if self.active_tool == "hand":
            shape = Qt.CursorShape.OpenHandCursor
        elif self.active_tool == "select" or self.active_tool in _TEXT_TOOLS:
            shape = Qt.CursorShape.IBeamCursor
        else:
            shape = Qt.CursorShape.CrossCursor
        self.viewport().setCursor(QCursor(shape))

    def mousePressEvent(self, event):  # noqa: N802 - Qt-API
        self.setFocus()
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        cx, cy = self._cxy(event)
        tool = self.active_tool
        if tool == "hand":
            self._clear_selection()
            self._draw = {"mode": "pan", "origin": event.position().toPoint(),
                          "scroll": (self.horizontalScrollBar().value(),
                                     self.verticalScrollBar().value())}
            self.viewport().setCursor(QCursor(Qt.CursorShape.ClosedHandCursor))
            return
        if tool == "select":
            # Markoer-vaerktoejet: rammer klikket en annotation, vaelges den;
            # ellers begynder en TEKST-markering.
            hit = self._annot_at(cx, cy)
            self._clear_text_selection()
            if hit:
                self._sel = hit
                self._draw = None
                self.viewport().update()
                return
            self._sel = None
            rec = self._page_at(cx, cy)
            if rec is not None and not self.has_text(rec):
                self._hint_no_text()
            self._draw = ({"mode": "text", "rec": rec, "x0": cx, "y0": cy, "hits": []}
                          if rec is not None else None)
            self.viewport().update()
            return
        rec = self._page_at(cx, cy)
        if rec is None:
            self._draw = None
            return
        if tool in _TEXT_TOOLS:
            if not self.has_text(rec):
                self._hint_no_text()
            self._draw = {"mode": "text", "rec": rec, "x0": cx, "y0": cy, "hits": []}
        else:
            self._draw = {"mode": "shape", "rec": rec, "x0": cx, "y0": cy,
                          "x1": cx, "y1": cy, "pts": [(cx, cy)]}

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt-API
        if not self._draw:
            return super().mouseMoveEvent(event)
        mode = self._draw["mode"]
        if mode == "pan":
            delta = event.position().toPoint() - self._draw["origin"]
            sx, sy = self._draw["scroll"]
            self.horizontalScrollBar().setValue(sx - delta.x())
            self.verticalScrollBar().setValue(sy - delta.y())
            return
        cx, cy = self._cxy(event)
        if mode == "text":
            self._update_text_sel(cx, cy)
            return
        self._draw["x1"], self._draw["y1"] = cx, cy
        if self.active_tool == "ink":
            self._draw["pts"].append((cx, cy))
        self.viewport().update()

    def _update_text_sel(self, cx, cy) -> None:
        """Opdater markeringen. ``hits`` er ét ``(rect, tekst)`` **pr. linje**.

        Ét rect pr. linje -- ikke pr. ord -- baade fordi markeringen saa ikke
        bliver til en stak halvgennemsigtige ord-rammer oven i hinanden, og fordi
        den faerdige fremhaevning saa daekker mellemrummene mellem ordene.
        """
        d = self._draw
        if (abs(cx - d["x0"]) < _DRAG_SLOP and abs(cy - d["y0"]) < _DRAG_SLOP):
            d["hits"] = []          # et rent klik markerer ikke noget
            self.viewport().update()
            return
        lines = d.get("lines")
        if lines is None:
            lines = d["lines"] = self._visual_lines(d["rec"])
        hits = []
        for run in self._line_runs(lines, (d["x0"], d["y0"]), (cx, cy)):
            xs = [v for _b, w in run for v in (w[0], w[2])]
            ys = [v for _b, w in run for v in (w[1], w[3])]
            hits.append(((min(xs), min(ys), max(xs), max(ys)),
                         " ".join(w[4] for _b, w in run)))
        d["hits"] = hits
        self.viewport().update()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt-API
        d, self._draw = self._draw, None
        if not d:
            return super().mouseReleaseEvent(event)
        mode = d["mode"]
        if mode == "pan":
            self._apply_cursor()
            self._render_timer.start(30)
            return
        cx, cy = self._cxy(event)
        rec = d["rec"]
        vt = rec["vt"]
        tool = self.active_tool
        if mode == "text":
            hits = d.get("hits", [])
            rects = [r for r, _w in hits]
            if tool == "select":
                # Markeringen bliver STAAENDE (modsat markup-vaerktoejerne, der
                # forbruger den med det samme), saa hoejreklik kan handle paa den.
                self._text_selection = ({"page_uid": rec["uid"],
                                         "rects": tuple(rects),
                                         "text": " ".join(w for _r, w in hits)}
                                        if rects else None)
                self.viewport().update()
                return
            if rects and tool in _TEXT_TOOLS:
                self._commit_text(rec["uid"], tool, rects)
            self.viewport().update()
            return

        # figur
        if tool == "ink":
            pts = d["pts"]
            if len(pts) < 2:
                self.viewport().update()
                return
            stroke = tuple(vt.canvas_to_pdf(px, py) for px, py in pts)
            self._commit(rec["uid"], em.AnnotationSpec(
                kind=an.ANNOT_INK, strokes=(stroke,), color=self.color, width=self.width))
            return
        if tool == "line":
            x0, y0 = vt.canvas_to_pdf(d["x0"], d["y0"])
            x1, y1 = vt.canvas_to_pdf(cx, cy)
            self._commit(rec["uid"], em.AnnotationSpec(
                kind=an.ANNOT_LINE, rects=((x0, y0, x1, y1),),
                color=self.color, width=self.width))
            return
        r = vt.rect_from_canvas(d["x0"], d["y0"], cx, cy)
        if (r[2] - r[0]) < 2 or (r[3] - r[1]) < 2:
            self.viewport().update()
            return
        if tool == "crop":
            # Beskaering gaelder KUN den side gestussen skete paa: et rektangel
            # fra en A4-portraetside er meningsloest paa en landskabsside.
            self.app.undo_stack.push(
                em.set_pages_crop_cmd(self.app.model, [(rec["uid"], r)]))
            self.rebuild()
            self.app.after_crop_change(rec["uid"])
            return
        if tool == "rect":
            self._commit(rec["uid"], em.AnnotationSpec(
                kind=an.ANNOT_RECT, rects=(r,), color=self.color, width=self.width))
        elif tool == "circle":
            self._commit(rec["uid"], em.AnnotationSpec(
                kind=an.ANNOT_CIRCLE, rects=(r,), color=self.color, width=self.width))
        elif tool == "redact":
            self._commit(rec["uid"], em.AnnotationSpec(
                kind=an.ANNOT_REDACT, rects=(r,), color=(0, 0, 0), fill=(0, 0, 0),
                source="manual"))
        elif tool == "freetext":
            dlg = _MultilineDialog(self, _("Tekst"), _("Skriv tekst:"))
            if dlg.exec() != QDialog.DialogCode.Accepted:
                return
            text = dlg.value()
            if text:
                fitted = self._fit_freetext(r, text, 11.0)
                self._commit(rec["uid"], em.AnnotationSpec(
                    kind=an.ANNOT_FREETEXT, rects=(fitted,), text=text,
                    color=self.color, fontsize=11.0))

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt-API
        cx, cy = self._cxy(event)
        hit = self._annot_at(cx, cy)
        if not hit:
            return
        page_uid, annot_uid = hit
        found = self.app.model.page_by_uid(page_uid)
        if not found:
            return
        _entry, page = found
        spec = next((a for a in page.annots if a.uid == annot_uid), None)
        if spec is None or spec.kind != an.ANNOT_FREETEXT:
            return
        dlg = _MultilineDialog(self, _("Tekst"), _("Skriv tekst:"), spec.text or "")
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        new_text = dlg.value()
        if new_text is None or new_text == spec.text:
            return
        r = spec.rects[0] if spec.rects else (0, 0, 100, 20)
        new_spec = em.AnnotationSpec(
            kind=an.ANNOT_FREETEXT, rects=(self._fit_freetext(r, new_text, spec.fontsize),),
            text=new_text, color=spec.color, fontsize=spec.fontsize)
        self._swap_annotation(page_uid, spec, new_spec, N_("Rediger tekst"))

    def contextMenuEvent(self, event):  # noqa: N802 - Qt-API
        """Hoejreklik i fremviseren. Er der markeret tekst, tilbydes handlinger
        paa den; ellers er der intet at vise."""
        text = self.selected_text()
        if not text:
            return
        short = (text[:28] + "…") if len(text) > 28 else text
        menu = QMenu(self)
        menu.addAction(_("Kopiér"), self.copy_selected_text)
        menu.addSeparator()
        menu.addAction(_("Fremhæv markering"),
                       lambda: self._commit_from_selection("highlight"))
        menu.addAction(_("Understreg markering"),
                       lambda: self._commit_from_selection("underline"))
        menu.addAction(_("Gennemstreg markering"),
                       lambda: self._commit_from_selection("strikeout"))
        menu.addAction(_("Masker markering"),
                       lambda: self._commit_from_selection("redact"))
        menu.addSeparator()
        menu.addAction(_("Masker alle forekomster af \"%s\"") % short,
                       lambda t=text: self._redact_all_of(t))
        menu.exec(event.globalPos())

    def _commit_from_selection(self, kind: str) -> None:
        """Lav en annotation ud af den staaende tekstmarkering."""
        sel = self._text_selection
        if not sel or not sel.get("rects"):
            return
        rects = tuple(sel["rects"])
        if kind == "redact":
            spec = em.AnnotationSpec(kind=an.ANNOT_REDACT, rects=rects,
                                     color=(0, 0, 0), fill=(0, 0, 0), source="manual")
        else:
            kinds = {"underline": an.ANNOT_UNDERLINE, "strikeout": an.ANNOT_STRIKEOUT,
                     "highlight": an.ANNOT_HIGHLIGHT}
            spec = em.AnnotationSpec(kind=kinds.get(kind, an.ANNOT_HIGHLIGHT),
                                     rects=rects, color=self.color)
        page_uid = sel["page_uid"]
        self._clear_text_selection()
        self._commit(page_uid, spec)

    def _redact_all_of(self, text: str) -> None:
        """Send teksten videre til sidevisningens soege-maskering (alle filer)."""
        self._clear_text_selection()
        view = getattr(self.app, "page_view", None)
        if view is not None:
            view.search_and_redact(text)

    # ------------------------------------------------------------ commits
    def _commit(self, page_uid, spec) -> None:
        self.app.undo_stack.push(em.add_annotation_cmd(self.app.model, page_uid, spec))
        self.viewport().update()

    def _commit_text(self, page_uid, tool, rects) -> None:
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

    # -------------------------------------------------------- zoom og scroll
    def _next_zoom(self, direction: int) -> float:
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

    def _set_zoom(self, z: float, anchor=None) -> None:
        """Zoom omkring et fast punkt -- som standard udsnittets CENTRUM.

        Ankerpunktet udtrykkes RELATIVT til en side, saa det overlever det nye
        layout, og saettes tilbage i centrum paa begge akser. Udtrykt i absolutte
        rullepositioner ville et zoom-spring flytte siden ud af billedet, naar
        dokumentets bredde skifter fra "smallere end udsnittet" til bredere.

        ``anchor`` er et (laerred_x, laerred_y)-punkt; udelades det, bruges midten.
        """
        z = max(ZOOM_MIN, min(ZOOM_MAX, z))
        if abs(z - self.zoom) < 1e-6:
            return
        vp = self.viewport()
        cw, ch = max(1, vp.width()), max(1, vp.height())
        o = self._origin()
        ax, ay = anchor if anchor else (o.x() + cw / 2.0, o.y() + ch / 2.0)
        rec = self._page_at(ax, ay) or self._nearest_page(ay)
        uid, fx, fy = None, 0.0, 0.0
        if rec is not None:
            uid = rec["uid"]
            # Bevidst IKKE klampet til [0,1]: falder centrum i mellemrummet
            # mellem to sider, beholder det sit forhold til ankersiden, og det er
            # lige praecis det der faar gestussen til at foeles stabil.
            fx = (ax - rec["x"]) / max(1.0, rec["w"])
            fy = (ay - rec["y"]) / max(1.0, rec["h"])

        self.zoom = z
        self._compute_layout()
        rec = self.by_uid.get(uid) if uid else None
        if rec is not None:
            self.horizontalScrollBar().setValue(
                int(max(0.0, rec["x"] + fx * rec["w"] - cw / 2.0)))
            self.verticalScrollBar().setValue(
                int(max(0.0, rec["y"] + fy * rec["h"] - ch / 2.0)))
        self._refresh_visible()
        self.zoom_changed.emit(self.zoom_percent())

    def _nearest_page(self, cy):
        """Siden hvis midte ligger taettest paa ``cy`` (naar centrum rammer et
        mellemrum mellem to sider)."""
        if not self.pages:
            return None
        return min(self.pages, key=lambda r: abs(r["y"] + r["h"] / 2.0 - cy))

    def goto_page(self, uid: str) -> None:
        """Scroll til en side UDEN at melde et sideskift tilbage.

        Uden daempningen opstod en feedback-loekke: markering -> goto_page ->
        scroll -> sideskift -> tilbage i markeringen. Normalt landede den paa
        samme uid, men rullepositionen KLAMPER ved sidste skaerm, saa naer
        dokumentets slutning pegede den paa en senere side -- og markeringen
        hoppede foran den man netop pilede hen til."""
        rec = self.by_uid.get(uid)
        if rec is None:
            return
        self._suppress_notify = True
        try:
            self.verticalScrollBar().setValue(max(0, rec["y"] - MARGIN))
            self._refresh_visible()
        finally:
            self._suppress_notify = False
        self._last_page = uid

    def _goto_index(self, i: int) -> None:
        if 0 <= i < len(self.pages):
            self.goto_page(self.pages[i]["uid"])

    def _page_step(self, direction: int) -> None:
        cur = self.current_page_uid()
        idx = next((k for k, p in enumerate(self.pages) if p["uid"] == cur), 0)
        self._goto_index(max(0, min(len(self.pages) - 1, idx + direction)))

    def wheelEvent(self, event):  # noqa: N802 - Qt-API
        self.rmgr.notify_activity()
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            # Zoom om MARKOEREN, ikke om centrum -- som i enhver PDF-laeser.
            o = self._origin()
            pos = event.position().toPoint()
            anchor = (pos.x() + o.x(), pos.y() + o.y())
            self._set_zoom(self._next_zoom(1 if event.angleDelta().y() > 0 else -1),
                           anchor=anchor)
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt-API
        key = event.key()
        if key == Qt.Key.Key_PageDown:
            self._page_step(1)
        elif key == Qt.Key.Key_PageUp:
            self._page_step(-1)
        elif key == Qt.Key.Key_Home:
            self._goto_index(0)
        elif key == Qt.Key.Key_End:
            self._goto_index(len(self.pages) - 1)
        elif key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.delete_selected()
        elif key == Qt.Key.Key_Escape:
            self._clear_selection()
            self._clear_text_selection()
            self.viewport().update()
        elif (key == Qt.Key.Key_C
              and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.copy_selected_text()
        else:
            super().keyPressEvent(event)
            return
        event.accept()

    # ---------------------------------------------------------- hjaelpere
    def _clear_text_selection(self) -> None:
        self._text_selection = None

    def _clear_selection(self) -> None:
        self._sel = None

    @staticmethod
    def _fit_freetext(rect, text, fontsize):
        """Voks kassens hoejde saa hele teksten faar plads ved ombrydning."""
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
