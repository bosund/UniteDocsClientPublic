"""Sidevisningen: appens eneste dokumentvisning.

Venstre: et rulbart gitter af sideminiaturer grupperet pr. kildefil. Hoejre: den
kontinuerlige fremviser (``page_canvas.PageCanvas``) med annotations- og
maskeringsvaerktoejer.

**Gitteret tegnes -- det bygges ikke af widgets.** 8.x lavede én ``tk.Frame``
med tre børn pr. synlig flise og rev dem ned igen ved hver scroll-tick
(virtualisering var noedvendig, fordi 1500 widgets frøs UI'et i ~8 s). Her er
gitteret ét ``QAbstractScrollArea``, hvor ``paintEvent`` tegner de celler der er
i udsnittet. Der findes dermed *ingen* fliswidgets at oprette, genbruge eller
destruere: scroll er ren maling, og markering er en gentegning af to rektangler.

Layoutet er stadig ren geometri (billigt selv for tusinder af sider), og
rendering gaar gennem :class:`~app.page_render.PageRenderManager`, hvis workere
aldrig roerer UI'et -- resultatet leveres paa UI-traaden via ``app._queue``.
"""

from __future__ import annotations

import datetime
import os
import time
from pathlib import Path

from PIL import Image
from PySide6.QtCore import (QMimeData, QPoint, QPointF, QRect, QRectF, QSize, Qt,
                            QTimer, Signal)
from PySide6.QtGui import (QColor, QDrag, QFontMetrics, QKeySequence, QPainter,
                           QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import (QAbstractItemView, QAbstractScrollArea,
                               QColorDialog, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu,
                               QApplication, QSizePolicy, QSlider, QSplitter, QStyle,
                               QStyleOptionViewItem, QStyledItemDelegate,
                               QToolButton, QVBoxLayout, QWidget)

from . import annotations as an
from . import edit_model as em
from . import icons_vector
from . import ocr_text
from . import page_render
from . import pdf_renderer
from . import pdf_utils
from . import qt_util
from . import redaction
from . import theme
from . import welcome
from .localization import LocalizationManager
from .logging_config import get_logger
from .page_canvas import PageCanvas
from .tooltip import Tooltip

logger = get_logger(__name__)
_ = LocalizationManager.get_text

# --- geometri -------------------------------------------------------------
# Basis-flisestoerrelsen. Den faktiske stoerrelse er ``base * skala`` og styres
# af skyderen under gitteret; alle afledte maal regnes derfor pr. instans og
# ikke som modulkonstanter.
TILE_IMG = (110, 140)          # renderet billedfelt inde i en flise (100 %)
TILE_PAD = 6
NUM_H = 18                     # stribe med sidetal
TILE_SCALE_MIN = 60            # procent
TILE_SCALE_MAX = 220
HEADER_H = 46      # to linjer navn/dato + luft
# Fluebenet i "Vaelg sider". Fast stoerrelse uanset miniature-skala: det er en
# kontrol man skal kunne ramme, ikke en del af sidebilledet.
CHECK_BOX = 16
CHECK_PAD = 4
PAD = 8
GROUP_GAP = 22     # ogsaa udtraeks-zonen ved traek-og-slip
# Baggrunds-prefetch: naar sidetallet er kendt, varmes miniature-cachen op ved
# lav prioritet, saa senere scroll er et cache-hit. Loftet er der, saa et kaempe
# dokument ikke renderer tusinder af sider op front (LRU'en ville smide dem ud).
PREFETCH_CAP = 400

DRAG_THRESHOLD = 5             # px foer et klik bliver til et traek
AUTOSCROLL_ZONE = 30           # px i top/bund der ruller under et traek
AUTOSCROLL_MS = 60
_PAGE_MIME = "application/x-unitedocs-pages"

# Maskeringsvaerktoejer farves altid roede (destruktive) -- se ``_tool_color``.
_REDACT_TOOLS = ("redact", "redact_text")


def pil_to_pixmap(img: Image.Image) -> QPixmap:
    return icons_vector.pil_to_qpixmap(img)


class ThumbnailGrid(QAbstractScrollArea):
    """Det tegnede sidegitter.

    Ejer layout, markering, traek-og-slip og maling. Kommunikerer opad gennem
    signaler, saa ``PageView`` kan holde sammen paa fremviseren uden at gitteret
    kender til den.
    """

    page_activated = Signal(str)     # uid -- vis siden i fremviseren
    selection_changed = Signal()
    context_page = Signal(str, QPoint)
    context_file = Signal(str, QPoint)
    external_drop = Signal(list, object)   # stier, fil-indeks (eller None)
    select_mode_changed = Signal(bool)     # "Vaelg sider" slaaet til/fra

    def __init__(self, parent, view: "PageView"):
        super().__init__(parent)
        self.view = view
        self.app = view.app
        self.render_mgr = view.app.page_render_mgr

        self.cells: list[dict] = []
        self.page_order: list[str] = []
        self.total_height = 0
        self.cols = 1
        self.tile_scale = 100          # procent; styres af skyderen
        self._apply_tile_scale()
        self._pix: dict[str, QPixmap] = {}      # uid -> tegnet miniature
        self._pix_key: dict[str, tuple] = {}    # uid -> noeglen billedet kom fra

        self.select_mode = False               # "Vaelg sider": flueben paa fliserne
        self._selected: list[str] = []          # ordnede side-uids
        self._anchor_uid: str | None = None     # Shift-omraadets anker
        self._selected_file: str | None = None  # udelukker sidemarkering
        self._drop_target = None
        self._drag_origin: QPoint | None = None
        self._drag_uid: str | None = None
        self._drop_highlight = False

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAcceptDrops(True)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().setSingleStep(24)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

        # Billed-forespoergsler droslet: under en hurtig scroll males gitteret
        # straks, men renderinger bestilles foerst naar scroll falder til ro, saa
        # koeen ikke fyldes med sider der ruller lige ud igen.
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._request_visible_renders)

        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.timeout.connect(self._prefetch)

        self._autoscroll = QTimer(self)
        self._autoscroll.timeout.connect(self._autoscroll_step)
        self._autoscroll_dir = 0

    # -------------------------------------------------------- flisestoerrelse
    def _apply_tile_scale(self) -> None:
        """Udled flise- og cellemaal af den aktuelle skala."""
        f = max(TILE_SCALE_MIN, min(TILE_SCALE_MAX, int(self.tile_scale))) / 100.0
        self.tile_img = (max(40, int(round(TILE_IMG[0] * f))),
                         max(50, int(round(TILE_IMG[1] * f))))
        self.cell_w = self.tile_img[0] + 2 * TILE_PAD + 10
        self.cell_h = self.tile_img[1] + NUM_H + 2 * TILE_PAD + 10

    def set_tile_scale(self, percent: int) -> None:
        """Skift miniature-stoerrelse.

        Stoerrelsen indgaar i render-cachens noegle (``box``), saa de gamle
        billeder passer ikke til det nye maal. Vi rydder derfor baade pixmaps og
        noegler og bestiller de synlige fliser igen -- en allerede cachet
        rendering i den NYE stoerrelse rammes stadig med det samme.
        """
        percent = max(TILE_SCALE_MIN, min(TILE_SCALE_MAX, int(percent)))
        if percent == self.tile_scale:
            return
        # Behold den oeverste synlige side i syne, saa gitteret ikke hopper.
        anchor = self._top_visible_uid()
        self.tile_scale = percent
        self._apply_tile_scale()
        self._pix.clear()
        self._pix_key.clear()
        self._compute_layout()
        if anchor:
            self.ensure_visible(anchor)
        self.viewport().update()
        self._render_timer.start(30)
        self._prefetch_timer.start(400)

    def _top_visible_uid(self):
        top = self._offset()
        for c in self.cells:
            if c["kind"] == "tile" and c["y"] + c["h"] >= top:
                return c["key"]
        return None

    # ------------------------------------------------------------- layout
    def rebuild(self) -> None:
        """Genberegn layoutet og gentegn. Billigt selv for tusinder af sider."""
        self.render_mgr.bump_generation()
        self._compute_layout()
        live = set(self.page_order)
        self._selected = [u for u in self._selected if u in live]
        if self._anchor_uid not in live:
            self._anchor_uid = self._selected[-1] if self._selected else None
        if self._selected_file and self.app.model.entry_by_iid(self._selected_file) is None:
            self._selected_file = None
        # Miniaturer for sider der ikke findes laengere maa ikke hobe sig op.
        for uid in [u for u in self._pix if u not in live]:
            self._pix.pop(uid, None)
            self._pix_key.pop(uid, None)
        self.viewport().update()
        self._render_timer.start(50)
        self._prefetch_timer.start(250)
        if not self._selected and not self._selected_file and self.page_order:
            self.select_and_reveal(self.page_order[0])

    def _compute_layout(self) -> None:
        width = max(1, self.viewport().width())
        self.cols = max(1, (width - 2 * PAD) // self.cell_w)
        cells: list[dict] = []
        page_order: list[str] = []
        y = PAD
        files = self.app.model.files
        if not files:
            self.cells = []
            self.page_order = []
            self.total_height = 60
            self._sync_scrollbar()
            return
        for entry in files:
            cells.append({"kind": "header", "key": "h:" + entry.iid,
                          "x": PAD, "y": y, "w": width - 2 * PAD, "h": HEADER_H,
                          "entry": entry, "page": None})
            y += HEADER_H
            items = list(entry.pages) if (entry.pages_loaded and entry.pages) else [None]
            for i, page in enumerate(items):
                col, row = i % self.cols, i // self.cols
                cells.append({
                    "kind": "tile" if page is not None else "locked",
                    "key": page.uid if page is not None else "lock:" + entry.iid,
                    "x": PAD + col * self.cell_w, "y": y + row * self.cell_h,
                    "w": self.cell_w, "h": self.cell_h,
                    "entry": entry, "page": page,
                    "num": self._page_number(entry, page, i)})
                if page is not None:
                    page_order.append(page.uid)
            nrows = (len(items) + self.cols - 1) // self.cols
            y += nrows * self.cell_h + GROUP_GAP
        self.cells = cells
        self.page_order = page_order
        self.total_height = y
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        sb = self.verticalScrollBar()
        page = max(1, self.viewport().height())
        sb.setPageStep(page)
        sb.setRange(0, max(0, self.total_height - page))

    @staticmethod
    def _page_number(entry, page, position) -> str:
        """Etiketten under en flise.

        Sider fra filen selv viser deres KILDE-sidetal (``src_index + 1``), saa
        man kan se hvad der er slettet. Indsatte sider har ingen kildeside i
        filen -- de viser deres plads i listen med et plus foran, saa tallene
        ikke kolliderer."""
        if page is None:
            return ""
        if os.path.normcase(page.src_path) == os.path.normcase(entry.path):
            return str(page.src_index + 1)
        return "+%d" % (position + 1)

    def page_number_label(self, uid: str) -> str | None:
        for c in self.cells:
            if c["kind"] == "tile" and c["key"] == uid:
                return c.get("num")
        return None

    def resizeEvent(self, event):  # noqa: N802 - Qt-API
        super().resizeEvent(event)
        self._compute_layout()
        self.viewport().update()
        self._render_timer.start(50)

    # ------------------------------------------------------------ maling
    def _offset(self) -> int:
        return self.verticalScrollBar().value()

    def _visible_cells(self, buffer: int | None = None):
        top = self._offset()
        h = self.viewport().height()
        if buffer is None:
            buffer = h
        lo, hi = top - buffer, top + h + buffer
        return [c for c in self.cells if not (c["y"] + c["h"] < lo or c["y"] > hi)]

    def paintEvent(self, event):  # noqa: N802 - Qt-API
        p = QPainter(self.viewport())
        p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        p.fillRect(self.viewport().rect(),
                   QColor(theme.C["accent_subtle"] if self._drop_highlight
                          else theme.C["bg"]))
        if not self.cells:
            p.setPen(QColor(theme.C["text_muted"]))
            p.setFont(theme.font("base"))
            p.drawText(QRect(PAD, PAD, self.viewport().width() - 2 * PAD, 40),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       _("Ingen sider at vise. Tilføj filer."))
            return

        dy = -self._offset()
        clip = self.viewport().rect().adjusted(0, -self.cell_h, 0, self.cell_h)
        for c in self.cells:
            r = QRect(c["x"], c["y"] + dy, c["w"], c["h"])
            if not clip.intersects(r):
                continue
            if c["kind"] == "header":
                self._paint_header(p, c, r)
            elif c["kind"] == "locked":
                self._paint_locked(p, c, r)
            else:
                self._paint_tile(p, c, r)
        self._paint_drop_indicator(p, dy)
        p.end()

    # -- filhoved: navn + oprettelsesdato + haengelaas ------------------
    @staticmethod
    def _fmt_date(iso: str) -> str:
        """``"YYYY-MM-DD"`` -> ``"25/12-2025"``. Tom hvis datoen mangler."""
        if not iso:
            return ""
        try:
            d = datetime.date.fromisoformat(iso)
        except (ValueError, TypeError):
            return ""
        # Ingen f-string i _() (projektregel); %-formatering med navngivne felter.
        return _("%(day)02d/%(month)02d-%(year)04d") % {
            "day": d.day, "month": d.month, "year": d.year}

    @staticmethod
    def _lock_icon_for(entry):
        """(ikonnavn, farve) for filens krypteringstilstand, eller None."""
        key = getattr(entry, "enc_key", "")
        if key == pdf_utils.ENC_ENCRYPTED:
            return "lock", theme.C["warning"]
        if key == pdf_utils.ENC_DECRYPTED:
            return "unlock", theme.C["text_muted"]
        return None

    def _paint_header(self, p: QPainter, c: dict, r: QRect) -> None:
        entry = c["entry"]
        selected = entry.iid == self._selected_file
        if selected:
            p.fillRect(r, QColor(theme.C["accent_subtle"]))
            p.setPen(QPen(QColor(theme.C["accent"]), 2))
            p.drawRect(r.adjusted(1, 1, -1, -1))

        x = r.left() + theme.SPACE["sm"]
        lock = self._lock_icon_for(entry)
        if lock is not None:
            name, color = lock
            size = theme.ICON["small"]
            pm = icons_vector.qpixmap(name, size, color)
            p.drawPixmap(x, r.top() + (r.height() - size) // 2, size, size, pm)
            x += size + theme.SPACE["sm"]

        # Datoen har FORTRINSRET: navnet forkortes foer datoen ofres.
        p.setFont(theme.font("strong"))
        fm = QFontMetrics(theme.font("strong"))
        avail = max(40, r.right() - x - theme.SPACE["md"])
        name = Path(entry.path).name
        date = self._fmt_date(entry.creation_date)
        tail = ("   " + date) if date else ""
        p.setPen(QColor(theme.C["text"]))
        if fm.horizontalAdvance(name + tail) <= avail:
            p.drawText(QRect(x, r.top(), avail, r.height()),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       name + tail)
            return
        # To linjer: saa meget af navnet som der er plads til, resten forkortet.
        line_h = fm.height() + 2
        top = r.top() + (r.height() - 2 * line_h) // 2
        cut = self._fit_prefix(name, avail, fm)
        first, rest = name[:cut], name[cut:]
        p.drawText(QRect(x, top, avail, line_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, first)
        room = max(0, avail - fm.horizontalAdvance(tail))
        second = (fm.elidedText(rest, Qt.TextElideMode.ElideRight, room) + tail
                  if room else (date or ""))
        p.drawText(QRect(x, top + line_h, avail, line_h),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, second)

    @staticmethod
    def _fit_prefix(text: str, avail: int, fm: QFontMetrics) -> int:
        """Antal tegn af ``text`` der kan staa paa ``avail`` px (binaersoegning)."""
        if fm.horizontalAdvance(text) <= avail:
            return len(text)
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fm.horizontalAdvance(text[:mid]) <= avail:
                lo = mid
            else:
                hi = mid - 1
        return lo

    # -- fliser ---------------------------------------------------------
    def _paint_tile(self, p: QPainter, c: dict, r: QRect) -> None:
        page = c["page"]
        selected = page.uid in self._selected
        # I hvile en diskret kant, ved valg et LET accent-fyld + accent-ring.
        p.fillRect(r, QColor(theme.C["accent_subtle"] if selected else theme.C["tile_bg"]))
        p.setPen(QPen(QColor(theme.C["accent"] if selected else theme.C["border"]),
                      2 if selected else 1))
        p.drawRect(r.adjusted(1, 1, -1, -1))

        box = QRect(r.left() + (r.width() - self.tile_img[0]) // 2, r.top() + TILE_PAD,
                    self.tile_img[0], self.tile_img[1])
        pm = self._pix.get(page.uid)
        if pm is not None and not pm.isNull():
            w = int(pm.width() / pm.devicePixelRatio())
            h = int(pm.height() / pm.devicePixelRatio())
            p.drawPixmap(box.left() + (box.width() - w) // 2,
                         box.top() + (box.height() - h) // 2, pm)
        else:
            # Hvidt papir mens siden renderes -- ikke et tomt hul.
            p.fillRect(box, QColor(theme.C["paper"]))
            p.setPen(QPen(QColor(theme.C["border"]), 1))
            p.drawRect(box)

        p.setFont(theme.font("small"))
        p.setPen(QColor(theme.C["text"]))
        p.drawText(QRect(r.left(), r.bottom() - NUM_H, r.width(), NUM_H),
                   Qt.AlignmentFlag.AlignCenter, c.get("num", ""))

        if self.select_mode:
            self._paint_check(p, self._check_rect(r), selected)

    def _paint_check(self, p: QPainter, box: QRect, on: bool) -> None:
        """Afkrydsningsfelt paa en flise. Males OVEN PAA miniaturen, saa det
        ogsaa kan ses paa en side der er hvid i hjoernet."""
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setBrush(QColor(theme.C["accent"] if on else theme.C["paper"]))
        p.setPen(QPen(QColor(theme.C["accent"] if on else theme.C["border"]), 1))
        p.drawRoundedRect(box, 3, 3)
        if on:
            p.setPen(QPen(QColor(theme.C["selection_fg"]), 2,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                          Qt.PenJoinStyle.RoundJoin))
            w, h = box.width(), box.height()
            p.drawPolyline([QPoint(box.left() + int(w * 0.24), box.top() + int(h * 0.52)),
                            QPoint(box.left() + int(w * 0.44), box.top() + int(h * 0.72)),
                            QPoint(box.left() + int(w * 0.78), box.top() + int(h * 0.28))])
        p.restore()

    def _paint_locked(self, p: QPainter, c: dict, r: QRect) -> None:
        p.fillRect(r, QColor(theme.C["tile_bg"]))
        p.setPen(QPen(QColor(theme.C["border"]), 1))
        p.drawRect(r.adjusted(1, 1, -1, -1))
        size = theme.ICON["cmdlg"]
        pm = icons_vector.qpixmap("lock", size, theme.C["warning"])
        p.drawPixmap(r.center().x() - size // 2, r.top() + TILE_PAD + 40, size, size, pm)
        p.setFont(theme.font("small"))
        p.setPen(QColor(theme.C["text_muted"]))
        p.drawText(QRect(r.left(), r.bottom() - NUM_H, r.width(), NUM_H),
                   Qt.AlignmentFlag.AlignCenter, _("Låst"))

    CARET = 4          # halv bredde paa indsaetnings-karetens "vinger"

    def _paint_drop_indicator(self, p: QPainter, dy: int) -> None:
        """Vis hvor de trukne sider lander -- utvetydigt.

        Tre lag, fordi en tynd streg alene er for nem at overse midt i et
        gitter af fliser:
          1. **maalgruppen** toenes let, saa man ser hvilken FIL siden havner i,
          2. **karetten** (bjaelke med vinger) viser den praecise plads,
          3. ved udtraek: et baand paa tvaers af hele gitteret i mellemrummet.

        I 8.x maatte indikatoren vaere en rigtig widget, fordi Tk altid tegner
        indlejrede vinduer oeverst. Her males alt i samme lag, saa den blot
        tegnes sidst.
        """
        t = self._drop_target
        if not t:
            return
        accent = QColor(theme.C["drop_line"])
        if t[0] == "extract":
            y = self._extract_band_y(t[1]) + dy
            x0, w = PAD, max(40, self.viewport().width() - 2 * PAD)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(accent)
            p.drawRoundedRect(QRectF(x0, y - 2, w, 4), 2, 2)
            # Vinger i hver ende, saa baandet ikke forveksles med en kant.
            for cx in (x0 + 2, x0 + w - 2):
                p.drawEllipse(QRectF(cx - 4, y - 4, 8, 8))
            return

        self._paint_target_group(p, t[1], dy, accent)
        geom = self._into_bar_geometry(t)
        if geom is None:
            return
        x, y, w, h = geom
        y += dy
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(x - 1, y, w + 2, h), 2, 2)
        # Trekantede kareter foroven og forneden -- samme sprog som en
        # tekstmarkoer, saa "her indsaettes" laeses med det samme.
        c = self.CARET
        mid = x + w / 2.0
        # PySide6's drawPolygon tager en SEKVENS -- ikke punkter som varargs.
        p.drawPolygon(QPolygonF([QPointF(mid - c - 1, y - c),
                                 QPointF(mid + c + 1, y - c),
                                 QPointF(mid, y + 2)]))
        p.drawPolygon(QPolygonF([QPointF(mid - c - 1, y + h + c),
                                 QPointF(mid + c + 1, y + h + c),
                                 QPointF(mid, y + h - 2)]))

    def _paint_target_group(self, p: QPainter, iid, dy: int, accent: QColor) -> None:
        """Ton den fil sidene lander i, saa maalet aldrig er i tvivl."""
        cells = [c for c in self.cells if c["entry"].iid == iid]
        if not cells:
            return
        top = min(c["y"] for c in cells)
        bottom = max(c["y"] + c["h"] for c in cells)
        r = QRectF(PAD - 4, top + dy - 4,
                   max(40, self.viewport().width() - 2 * PAD + 8),
                   bottom - top + 8)
        tint = QColor(accent)
        tint.setAlpha(28)
        p.setPen(QPen(accent, 1, Qt.PenStyle.DashLine))
        p.setBrush(tint)
        p.drawRoundedRect(r, 6, 6)
        p.setBrush(Qt.BrushStyle.NoBrush)

    def _into_bar_geometry(self, target):
        """Lodret bjaelke praecis paa den plads siden indsaettes."""
        _k, iid, visual = target
        tiles = [c for c in self.cells if c["kind"] == "tile" and c["entry"].iid == iid]
        if not tiles:
            hdr = next((c for c in self.cells
                        if c["kind"] == "header" and c["entry"].iid == iid), None)
            return None if hdr is None else (hdr["x"], hdr["y"], 3, hdr["h"])
        if visual < len(tiles):
            cell = tiles[visual]
            x = cell["x"]
        else:
            cell = tiles[-1]
            x = cell["x"] + cell["w"] - 4
        return (x, cell["y"] + 6, 4, cell["h"] - 12)

    def _extract_band_y(self, file_index) -> int:
        """Y paa udtraeks-baandet foran den givne fil (eller efter den sidste)."""
        for c in self.cells:
            if c["kind"] != "header":
                continue
            if self.app.model.index_of_iid(c["entry"].iid) == file_index:
                return max(2, c["y"] - GROUP_GAP // 2)
        return max(2, self.total_height - GROUP_GAP // 2)

    # --------------------------------------------------------- rendering
    def tile_key(self, page):
        """Render-cachens noegle for en sides flise.

        ``tile_img`` indgaar, fordi stoerrelsen kan aendres af skyderen -- to
        skalaer maa ikke dele samme cache-post."""
        return page_render.cache_key(page.src_path, page.src_index, page.rotation,
                                     self.tile_img, page.crop)

    def _on_scrolled(self) -> None:
        self.render_mgr.notify_activity()   # pause prefetch, saa scroll er glat
        self.viewport().update()
        self._render_timer.start(50)

    def _request_visible_renders(self) -> None:
        gen = self.render_mgr._current_generation()
        pw = self.app._get_all_passwords()
        for c in self._visible_cells():
            if c["kind"] != "tile":
                continue
            page = c["page"]
            key = self.tile_key(page)
            # Sammenlign med den noegle flisens billede blev tegnet FRA -- ellers
            # ville en roteret eller beskaaret side aldrig faa nyt billede.
            if self._pix_key.get(page.uid) == key:
                continue
            cached = self.render_mgr.cache.get(key)
            if cached is not None:
                self._set_tile_image(page.uid, cached)
                continue
            self.render_mgr.request(page.uid, page.src_path, pw, page.src_index,
                                    page.rotation, self.tile_img, self.on_tile_ready,
                                    priority=1, generation=gen, crop=page.crop)

    def _prefetch(self) -> None:
        """Varm cachen op ved lav prioritet. Synlige fliser (pri 1) og
        fremviseren (pri 0) fortraenger altid dette (pri 2)."""
        if not self.cells:
            return
        gen = self.render_mgr._current_generation()
        pw = self.app._get_all_passwords()
        count = 0
        for c in self.cells:
            if c["kind"] != "tile":
                continue
            page = c["page"]
            if self.render_mgr.cache.get(self.tile_key(page)) is not None:
                continue
            self.render_mgr.request(page.uid, page.src_path, pw, page.src_index,
                                    page.rotation, self.tile_img, self.on_tile_ready,
                                    priority=2, generation=gen, crop=page.crop)
            count += 1
            if count >= PREFETCH_CAP:
                break

    def _set_tile_image(self, uid: str, pil) -> None:
        if pil is None:
            return
        found = self.app.model.page_by_uid(uid)
        if found is None:
            return
        self._pix[uid] = pil_to_pixmap(pil)
        self._pix_key[uid] = self.tile_key(found[1])
        self.viewport().update()

    def on_tile_ready(self, uid, pil) -> None:
        """Kaldes paa UI-traaden af render-manageren."""
        self._set_tile_image(uid, pil)

    # --------------------------------------------------------- markering
    def selected_uids(self) -> list:
        """Markerede sider i modelraekkefoelge (tom hvis en fil er valgt)."""
        order = {u: i for i, u in enumerate(self.page_order)}
        return sorted((u for u in self._selected if u in order), key=order.get)

    def selected_file(self) -> str | None:
        return self._selected_file

    def selected_page_uid(self) -> str | None:
        sel = self.selected_uids()
        return sel[-1] if sel else None

    def set_select_mode(self, on: bool) -> None:
        """Slaa "Vaelg sider" til eller fra.

        Tilstanden aendrer **ikke** markeringsmodellen -- ``_selected`` har altid
        vaeret en liste. Den goer to ting: hver flise faar et flueben man kan se
        og ramme, og et almindeligt klik bliver additivt. Det er dét der goer
        flervalg brugbart uden tastatur.
        """
        on = bool(on)
        if on == self.select_mode:
            return
        self.select_mode = on
        if not on and len(self._selected) > 1:
            # Slaas fluebenene fra, skal flermarkeringen ogsaa vaek. Ellers ville
            # "Flet og gem" arbejde paa en markering brugeren ikke laengere kan
            # se -- usynlig tilstand der styrer en destruktiv handling.
            self._selected = self._selected[-1:]
            self._anchor_uid = self._selected[0]
            self.selection_changed.emit()
        self.viewport().update()
        self.select_mode_changed.emit(on)

    def _check_rect(self, r: QRect) -> QRect:
        """Fluebenets felt i en flise -- oeverste venstre hjoerne."""
        size = CHECK_BOX
        return QRect(r.left() + CHECK_PAD, r.top() + CHECK_PAD, size, size)

    def select(self, uid, *, additive=False, extend=False, reveal_in_viewer=True) -> None:
        """Marker en side. ``additive`` = Ctrl-klik, ``extend`` = Shift-klik."""
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
        self.viewport().update()
        if reveal_in_viewer and not extend and not additive:
            self.page_activated.emit(uid)
        self.selection_changed.emit()

    def select_file(self, iid: str) -> None:
        """Marker en HEL fil (klik paa filhovedet). Rydder sidemarkeringen."""
        self._selected = []
        self._anchor_uid = None
        self._selected_file = iid
        self.viewport().update()
        self.selection_changed.emit()

    def select_all_in_file(self, iid: str) -> None:
        """Marker alle sider i filen (praktisk foran roter/slet/traek)."""
        entry = self.app.model.entry_by_iid(iid)
        if entry is None or not entry.pages:
            return
        want = {p.uid for p in entry.pages}
        self._selected = [u for u in self.page_order if u in want]
        self._anchor_uid = self._selected[0] if self._selected else None
        self._selected_file = None
        if len(self._selected) > 1:
            self.set_select_mode(True)
        self.viewport().update()
        self.selection_changed.emit()

    def reselect(self, uids) -> None:
        """Genskab en markering efter en rebuild (uids overlever kommandoerne)."""
        live = set(self.page_order)
        keep = {u for u in uids if u in live}
        if not keep:
            return
        self._selected = [u for u in self.page_order if u in keep]
        self._anchor_uid = self._selected[0]
        self._selected_file = None
        self.ensure_visible(self._selected[0])
        self.viewport().update()
        self.selection_changed.emit()

    def select_and_reveal(self, uid: str) -> None:
        self.ensure_visible(uid)
        self.select(uid)

    def ensure_visible(self, uid: str) -> None:
        cell = next((c for c in self.cells if c["key"] == uid), None)
        if not cell:
            return
        sb = self.verticalScrollBar()
        top, h = sb.value(), self.viewport().height()
        if cell["y"] < top:
            sb.setValue(max(0, cell["y"] - PAD))
        elif cell["y"] + cell["h"] > top + h:
            sb.setValue(max(0, cell["y"] + cell["h"] - h + PAD))

    # -------------------------------------------------------- interaktion
    def _cell_at(self, pos: QPoint):
        x, y = pos.x(), pos.y() + self._offset()
        for c in self.cells:
            if (c["x"] <= x <= c["x"] + c["w"] and c["y"] <= y <= c["y"] + c["h"]):
                return c
        return None

    def mousePressEvent(self, event):  # noqa: N802 - Qt-API
        self.setFocus()
        cell = self._cell_at(event.position().toPoint())
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        if cell is None:
            return super().mousePressEvent(event)
        mods = event.modifiers()
        if cell["kind"] == "header":
            self.select_file(cell["entry"].iid)
            return
        if cell["kind"] == "locked":
            return
        uid = cell["key"]
        if mods & Qt.KeyboardModifier.ControlModifier:
            # Ctrl-klik ER flervalg. Slaa fluebenene til med det samme, saa
            # brugeren kan SE hvad der er valgt -- og fortsaette uden tastatur.
            self.set_select_mode(True)
            self.select(uid, additive=True)
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            self.set_select_mode(True)
            self.select(uid, extend=True)
        elif self.select_mode:
            # I "Vaelg sider" er et almindeligt klik additivt. Traekket bevares,
            # men KUN naar klikket lagde siden TIL markeringen: fravaelger man en
            # side, ville et efterfoelgende traek flytte noget man lige har
            # sagt fra.
            self.select(uid, additive=True)
            if uid in self._selected:
                self._drag_origin = event.position().toPoint()
                self._drag_uid = uid
        else:
            if uid not in self._selected:
                self.select(uid)
            self._drag_origin = event.position().toPoint()
            self._drag_uid = uid

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt-API
        if self._drag_origin is None or not (event.buttons() & Qt.MouseButton.LeftButton):
            return super().mouseMoveEvent(event)
        if (event.position().toPoint() - self._drag_origin).manhattanLength() < DRAG_THRESHOLD:
            return
        uid, self._drag_origin = self._drag_uid, None
        if uid is None:
            return
        if uid not in self._selected:
            self.select(uid)
        self._start_page_drag(self.selected_uids())

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt-API
        self._drag_origin = None
        self._drag_uid = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event):  # noqa: N802 - Qt-API
        cell = self._cell_at(event.position().toPoint())
        if cell is not None and cell["kind"] == "tile":
            self.page_activated.emit(cell["key"])

    def contextMenuEvent(self, event):  # noqa: N802 - Qt-API
        cell = self._cell_at(event.pos())
        if cell is None:
            return
        if cell["kind"] == "header":
            self.select_file(cell["entry"].iid)
            self.context_file.emit(cell["entry"].iid, event.globalPos())
        elif cell["kind"] == "tile":
            # Et hoejreklik inde i en eksisterende markering bevarer den (som i
            # Stifinder); ellers markerer det den flise man ramte.
            if cell["key"] not in self._selected:
                self.select(cell["key"])
            self.context_page.emit(cell["key"], event.globalPos())

    def wheelEvent(self, event):  # noqa: N802 - Qt-API
        self.render_mgr.notify_activity()
        super().wheelEvent(event)

    def keyPressEvent(self, event):  # noqa: N802 - Qt-API
        key = event.key()
        step = {Qt.Key.Key_Down: self.cols, Qt.Key.Key_Up: -self.cols,
                Qt.Key.Key_Right: 1, Qt.Key.Key_Left: -1}.get(key)
        if step is None:
            if key == Qt.Key.Key_PageDown:
                step = self._page_step()
            elif key == Qt.Key.Key_PageUp:
                step = -self._page_step()
            elif key == Qt.Key.Key_Home:
                step = -10 ** 9
            elif key == Qt.Key.Key_End:
                step = 10 ** 9
        if step is not None:
            self._navigate(step)
            event.accept()
            return
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.view.delete_selected_key()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.SelectAll):
            self._selected = list(self.page_order)
            self._anchor_uid = self._selected[0] if self._selected else None
            self._selected_file = None
            if len(self._selected) > 1:
                self.set_select_mode(True)
            self.viewport().update()
            self.selection_changed.emit()
            event.accept()
            return
        if key == Qt.Key.Key_Escape and self.select_mode:
            self.set_select_mode(False)
            event.accept()
            return
        super().keyPressEvent(event)

    def _page_step(self) -> int:
        rows = max(1, self.viewport().height() // self.cell_h)
        return max(1, self.cols * rows)

    def _navigate(self, step: int) -> None:
        if not self.page_order:
            return
        self.render_mgr.notify_activity()
        cur = self.selected_page_uid()
        if cur in self.page_order:
            i = self.page_order.index(cur)
            i = max(0, min(len(self.page_order) - 1, i + step))
        else:
            i = 0 if step >= 0 else len(self.page_order) - 1
        self.select_and_reveal(self.page_order[i])

    # -------------------------------------------------- traek og slip
    def _start_page_drag(self, uids: list[str]) -> None:
        """Start et internt sidetraek.

        Qt's ``QDrag`` giver traek-billedet, markoeren og selve traek-loekken --
        8.x maatte selv lave et ``overrideredirect``-Toplevel, flytte det ved
        hver musebevaegelse og huske paa PhotoImage-referencer."""
        if not uids:
            return
        mime = QMimeData()
        mime.setData(_PAGE_MIME, ("\n".join(uids)).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.setPixmap(self._drag_pixmap(uids))
        drag.setHotSpot(QPoint(20, 16))
        drag.exec(Qt.DropAction.MoveAction)
        self._drop_target = None
        self._stop_autoscroll()
        self.viewport().update()

    def _drag_pixmap(self, uids: list[str]) -> QPixmap:
        """Miniature af den foerste side, med et antalsmaerke naar der er flere."""
        base = None
        found = self.app.model.page_by_uid(uids[0])
        if found is not None:
            pil = self.render_mgr.cache.get(self.tile_key(found[1]))
            if pil is not None:
                base = pil_to_pixmap(pil)
        if base is None or base.isNull():
            base = QPixmap(90, 60)
            base.fill(QColor(theme.C["surface"]))
        pm = QPixmap(base.size())
        pm.setDevicePixelRatio(base.devicePixelRatio())
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setOpacity(0.85)
        p.drawPixmap(0, 0, base)
        p.setOpacity(1.0)
        p.setPen(QPen(QColor(theme.C["border_strong"]), 1))
        p.drawRect(0, 0, pm.width() - 1, pm.height() - 1)
        if len(uids) > 1:
            badge = "+%d" % (len(uids) - 1)
            p.setFont(theme.font("small"))
            fm = QFontMetrics(theme.font("small"))
            w = fm.horizontalAdvance(badge) + 8
            r = QRect(pm.width() - w - 2, 2, w, fm.height() + 2)
            p.fillRect(r, QColor(theme.C["danger"]))
            p.setPen(QColor(theme.C["selection_fg"]))
            p.drawText(r, Qt.AlignmentFlag.AlignCenter, badge)
        p.end()
        return pm

    def _drop_target_at(self, pos: QPoint):
        """-> ("into", file_iid, position) | ("extract", file_index) | None."""
        x, y = pos.x(), pos.y() + self._offset()
        for cell in self.cells:
            if not (cell["x"] <= x <= cell["x"] + cell["w"]
                    and cell["y"] <= y <= cell["y"] + cell["h"]):
                continue
            entry = cell["entry"]
            if cell["kind"] == "header":
                return ("into", entry.iid, 0)
            if cell["kind"] == "locked":
                return None
            try:
                pos_in = entry.pages.index(cell["page"])
            except ValueError:
                return None
            # Venstre halvdel = foer flisen, hoejre halvdel = efter.
            if x > cell["x"] + cell["w"] / 2:
                pos_in += 1
            return ("into", entry.iid, pos_in)
        # Ikke over en celle: mellemrummet mellem to filgrupper betyder "riv ud
        # som egen fil". GROUP_GAP er bevidst bred nok til at kunne rammes.
        return ("extract", self._file_index_at(y))

    def _file_index_at(self, y) -> int:
        idx = len(self.app.model.files)
        for cell in self.cells:
            if cell["kind"] != "header":
                continue
            if y < cell["y"]:
                idx = self.app.model.index_of_iid(cell["entry"].iid)
                break
        return max(0, idx)

    def drop_file_index(self, global_pos: QPoint):
        """Fil-indeks for et Explorer-drop paa de givne skaermkoordinater, eller
        None hvis punktet ikke er over gitteret."""
        pos = self.viewport().mapFromGlobal(global_pos)
        if not self.viewport().rect().contains(pos):
            return None
        target = self._drop_target_at(pos)
        if target is None:
            return None
        if target[0] == "extract":
            return target[1]
        idx = self.app.model.index_of_iid(target[1])
        return None if idx < 0 else idx

    def set_drop_highlight(self, on: bool) -> None:
        self._drop_highlight = bool(on)
        self.viewport().update()

    def dragEnterEvent(self, event):  # noqa: N802 - Qt-API
        mime = event.mimeData()
        if mime.hasFormat(_PAGE_MIME):
            event.acceptProposedAction()
        elif mime.hasUrls():
            self.set_drop_highlight(True)
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 - Qt-API
        mime = event.mimeData()
        pos = event.position().toPoint()
        if mime.hasFormat(_PAGE_MIME):
            self._drop_target = self._drop_target_at(pos)
            if self._drop_target and self._drop_target[0] == "extract":
                self.app.set_status(_("Slip for at gøre siden til sin egen fil"))
            self._maybe_autoscroll(pos)
            self.viewport().update()
            event.acceptProposedAction()
        elif mime.hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):  # noqa: N802 - Qt-API
        self._drop_target = None
        self._stop_autoscroll()
        self.set_drop_highlight(False)
        self.viewport().update()
        event.accept()

    def dropEvent(self, event):  # noqa: N802 - Qt-API
        mime = event.mimeData()
        self._stop_autoscroll()
        self.set_drop_highlight(False)
        if mime.hasFormat(_PAGE_MIME):
            target, self._drop_target = self._drop_target, None
            uids = bytes(mime.data(_PAGE_MIME)).decode("utf-8").split("\n")
            self.viewport().update()
            self.view.commit_page_drop(uids, target)
            event.acceptProposedAction()
            return
        if mime.hasUrls():
            paths = [u.toLocalFile() for u in mime.urls() if u.toLocalFile()]
            local = event.position().toPoint()
            target = self._drop_target_at(local)
            if target is None:
                idx = None
            elif target[0] == "extract":
                idx = target[1]
            else:
                found = self.app.model.index_of_iid(target[1])
                idx = None if found < 0 else found
            self.external_drop.emit(paths, idx)
            event.acceptProposedAction()

    # --- auto-scroll under traek -----------------------------------------
    def _maybe_autoscroll(self, pos: QPoint) -> None:
        h = self.viewport().height()
        if pos.y() < AUTOSCROLL_ZONE:
            self._autoscroll_dir = -1
        elif pos.y() > h - AUTOSCROLL_ZONE:
            self._autoscroll_dir = 1
        else:
            self._stop_autoscroll()
            return
        if not self._autoscroll.isActive():
            self._autoscroll.start(AUTOSCROLL_MS)

    def _autoscroll_step(self) -> None:
        sb = self.verticalScrollBar()
        sb.setValue(sb.value() + self._autoscroll_dir * sb.singleStep())

    def _stop_autoscroll(self) -> None:
        self._autoscroll.stop()
        self._autoscroll_dir = 0

    def cancel_drag(self) -> None:
        self._drop_target = None
        self._drag_origin = None
        self._stop_autoscroll()
        self.viewport().update()


class _FileItemDelegate(QStyledItemDelegate):
    """Tegner en filpost som to linjer: navnet, og metadata daempet under.

    Navnet og metalinjen laeses fra hver sin **item-rolle**, ikke fra én
    ``"navn\\nmeta"``-streng. Det er ikke kosmetik: proppet ned i ``DisplayRole``
    blev linjeskiftet fladet ud af viewets elide-tilstand, saa posten kom ud som
    ``"Rapport.pd... - 4,3 KB"`` paa én linje. To roller kan ikke flettes.

    Baggrunden (hover/markering) tegnes stadig af stilen, saa QSS'en gaelder.
    """

    META = Qt.ItemDataRole.UserRole + 1

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""                       # vi tegner selv teksten
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)

        r = opt.rect.adjusted(8, 5, -8, -5)
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if icon is not None and not icon.isNull():
            sz = theme.ICON["small"]
            icon.paint(painter, QRect(r.left(), r.top() + (r.height() - sz) // 2, sz, sz))
            r.setLeft(r.left() + sz + theme.SPACE["sm"])

        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        meta = str(index.data(self.META) or "")
        fm_name = QFontMetrics(theme.font("base"))
        fm_meta = QFontMetrics(theme.font("small"))

        painter.save()
        painter.setFont(theme.font("base"))
        painter.setPen(QColor(theme.C["text"]))
        painter.drawText(
            QRect(r.left(), r.top(), r.width(), fm_name.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            fm_name.elidedText(name, Qt.TextElideMode.ElideMiddle, r.width()))
        painter.setFont(theme.font("small"))
        painter.setPen(QColor(theme.C["text_muted"]))
        painter.drawText(
            QRect(r.left(), r.top() + fm_name.height(), r.width(), fm_meta.height()),
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            fm_meta.elidedText(meta, Qt.TextElideMode.ElideRight, r.width()))
        painter.restore()

    def sizeHint(self, option, index):
        # Hoejden skal rumme BEGGE linjer plus luft. QSS'ens ``margin`` traekkes
        # fra bagefter, saa der maa vaere plads til den ogsaa.
        h = (QFontMetrics(theme.font("base")).height()
             + QFontMetrics(theme.font("small")).height() + 18)
        return QSize(120, h)


class FileListPanel(QListWidget):
    """Smal liste over de aabne filer.

    Sidegitteret er fint til at arbejde i, men uoverskueligt naar der er mange
    filer: filhovederne ligger spredt ud mellem hundredvis af fliser. Listen her
    er et fast indeks -- klik paa en fil for at markere den og rulle hen til den.

    Den genindfoerer **ikke** den gamle filvisning: den er en navigation, ikke en
    redigeringsflade. Al mutation gaar fortsat gennem gitteret og modellen.
    """

    file_chosen = Signal(str)
    files_reordered = Signal(list)         # ny iid-raekkefoelge
    external_drop = Signal(list, object)   # stier, indsaettelses-indeks

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._syncing = False
        self.setObjectName("FileList")
        # Traek-og-slip: omordn filer ved at traekke dem, og tag imod filer der
        # slippes fra Stifinder. Vi bruger IKKE ``InternalMove``: Qt ville selv
        # flytte raekkerne, og saa ville listen og modellen vaere ude af trit
        # indtil naeste rebuild -- og flytningen kunne ikke fortrydes.
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        # **CopyAction, ikke MoveAction.** ``QAbstractItemView.startDrag``
        # fjerner selv den trukne raekke, naar traekket ender som et
        # ``MoveAction`` -- og det sker EFTER vores ``dropEvent``, altsaa oven i
        # den liste vi lige har genopbygget fra modellen. Resultatet var at en
        # fil forsvandt fra listen (men blev i modellen). Vi flytter selv i
        # modellen, saa Qt skal holde fingrene fra raekkerne.
        self.setDefaultDropAction(Qt.DropAction.CopyAction)
        # Qt's egen drop-indikator er en haarfin streg, der naesten forsvinder i
        # windows11-stilen. Vi tegner vores egen (se ``paintEvent``).
        self.setDropIndicatorShown(False)
        self._drop_row_hint = None
        self._dragging_iid = None
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setUniformItemSizes(False)
        self.setWordWrap(False)
        # Delegaten eliderer selv hver linje for sig; viewets egen elide ville
        # gaelde hele posten under ét.
        self.setTextElideMode(Qt.TextElideMode.ElideNone)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setItemDelegate(_FileItemDelegate(self))
        self.itemSelectionChanged.connect(self._on_selection)

    def rebuild(self) -> None:
        """Genopbyg listen fra modellen og bevar markeringen.

        Hver post er **navn** paa foerste linje og **oprettelsesdato · stoerrelse**
        paa anden -- de tre ting man skelner to ens navngivne filer paa. Sidetallet
        staar i tooltippet sammen med den fulde sti; det kan man se i gitteret.
        """
        current = None
        item = self.currentItem()
        if item is not None:
            current = item.data(Qt.ItemDataRole.UserRole)
        self.blockSignals(True)
        self._syncing = True
        try:
            self.clear()
            for entry in self.app.model.files:
                item = QListWidgetItem(Path(entry.path).name)
                item.setData(_FileItemDelegate.META, self._meta_line(entry))
                item.setData(Qt.ItemDataRole.UserRole, entry.iid)
                item.setToolTip(self._tooltip(entry))
                lock = ThumbnailGrid._lock_icon_for(entry)
                if lock is not None:
                    name, color = lock
                    item.setIcon(icons_vector.qicon(name, theme.ICON["small"], color))
                self.addItem(item)
                if entry.iid == current:
                    self.setCurrentItem(item)
        finally:
            self._syncing = False
            self.blockSignals(False)

    @staticmethod
    def _page_count(entry) -> int:
        if entry.pages_loaded:
            return len(entry.pages)
        if entry.kind == em.KIND_IMAGE:
            return 1
        return entry.source_page_count

    @staticmethod
    def _meta_line(entry) -> str:
        """``"25/12-2025 · 1,4 MB"``. Felter der endnu ikke er laest udelades,
        saa linjen aldrig viser en tom plads eller et 0."""
        parts = []
        date = ThumbnailGrid._fmt_date(entry.creation_date)
        if date:
            parts.append(date)
        size = int(getattr(entry, "size_bytes", 0) or 0)
        if size:
            parts.append(qt_util.fmt_bytes(size))
        return "  ·  ".join(parts) if parts else _("Læser…")

    def _tooltip(self, entry) -> str:
        n = self._page_count(entry)
        pages = _("%(n)d sider") % {"n": n} if n != 1 else _("1 side")
        return "%s\n%s" % (entry.path, pages)

    def select_file(self, iid) -> None:
        """Afspejl gitterets markering uden at sende signalet retur."""
        self._syncing = True
        try:
            if iid is None:
                self.clearSelection()
                self.setCurrentItem(None)
            else:
                for i in range(self.count()):
                    it = self.item(i)
                    if it.data(Qt.ItemDataRole.UserRole) == iid:
                        self.setCurrentItem(it)
                        break
        finally:
            self._syncing = False

    def _on_selection(self) -> None:
        if self._syncing:
            return
        item = self.currentItem()
        if item is not None:
            self.file_chosen.emit(item.data(Qt.ItemDataRole.UserRole))

    # ----------------------------------------------------------- traek/slip
    def _drop_row(self, pos) -> int:
        """Hvilken raekke et slip paa ``pos`` betyder "indsaet foran"."""
        item = self.itemAt(pos)
        if item is None:
            return self.count()
        row = self.row(item)
        r = self.visualItemRect(item)
        return row + 1 if pos.y() > r.center().y() else row

    def startDrag(self, supported_actions):  # noqa: N802 - Qt-API
        """Husk hvad der traekkes, og koer traekket som en KOPI.

        ``currentItem()`` er ikke paalideligt som "den trukne post": traekker man
        en post uden foerst at markere den, peger den paa noget andet."""
        item = self.currentItem()
        self._dragging_iid = (item.data(Qt.ItemDataRole.UserRole)
                              if item is not None else None)
        super().startDrag(Qt.DropAction.CopyAction)
        self._dragging_iid = None
        self._set_drop_row(None)

    def dragEnterEvent(self, event):  # noqa: N802 - Qt-API
        mime = event.mimeData()
        if event.source() is self or mime.hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):  # noqa: N802 - Qt-API
        if event.source() is self or event.mimeData().hasUrls():
            self._set_drop_row(self._drop_row(event.position().toPoint()))
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):  # noqa: N802 - Qt-API
        self._set_drop_row(None)
        event.accept()

    def _set_drop_row(self, row) -> None:
        if row != self._drop_row_hint:
            self._drop_row_hint = row
            self.viewport().update()

    def paintEvent(self, event):  # noqa: N802 - Qt-API
        super().paintEvent(event)
        row = self._drop_row_hint
        if row is None:
            return
        # Indsaetnings-karet paa tvaers af listen, med vinger i begge ender, saa
        # det er tydeligt MELLEM hvilke to filer der slippes.
        from PySide6.QtGui import QPainter as _QP
        p = _QP(self.viewport())
        accent = QColor(theme.C["drop_line"])
        w = self.viewport().width()
        if self.count() == 0:
            y = 4
        elif row >= self.count():
            y = self.visualItemRect(self.item(self.count() - 1)).bottom()
        else:
            y = self.visualItemRect(self.item(row)).top()
        y = max(2, min(self.viewport().height() - 3, y))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(accent)
        p.drawRoundedRect(QRectF(6, y - 1.5, w - 12, 3), 1.5, 1.5)
        p.drawEllipse(QRectF(2, y - 4, 8, 8))
        p.drawEllipse(QRectF(w - 10, y - 4, 8, 8))
        p.end()

    def dropEvent(self, event):  # noqa: N802 - Qt-API
        pos = event.position().toPoint()
        row = self._drop_row(pos)
        self._set_drop_row(None)
        if event.source() is self:
            iid = self._dragging_iid
            if iid is None:
                item = self.currentItem()
                iid = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if iid is None:
                event.ignore()
                return
            self.files_reordered.emit([iid, row])
            # Ikke ``acceptProposedAction`` (= Move): saa ville view'et fjerne
            # raekken bagefter. Se kommentaren ved setDefaultDropAction.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        urls = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        if urls:
            self.external_drop.emit(urls, row)
            event.acceptProposedAction()
        else:
            event.ignore()


class PageView(QWidget):
    """Filliste + sidegitter + kontinuerlig fremviser i et delt panel."""

    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.render_mgr = app.page_render_mgr
        self.active_tool = "hand"
        self._anno_color = (1.0, 0.0, 0.0)
        self._tool_buttons: dict[str, QToolButton] = {}
        self._tool_icons: dict[str, str] = {}
        self._plain_icons: list = []

        lay = QVBoxLayout(self)
        lay.setContentsMargins(theme.SPACE["md"], theme.SPACE["sm"],
                               theme.SPACE["md"], theme.SPACE["sm"])
        lay.setSpacing(theme.SPACE["sm"])

        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        # Fillisten MAA kunne klappes helt sammen -- den er en bekvemmelighed,
        # ikke en fast del af fladen. De to andre ruder maa ikke.
        self.splitter.setChildrenCollapsible(True)
        lay.addWidget(self.splitter, 1)

        self.file_list = FileListPanel(self.splitter, app)
        self.file_list.file_chosen.connect(self._on_file_chosen)
        self.file_list.files_reordered.connect(self._on_files_reordered)
        self.file_list.external_drop.connect(self._on_external_drop)
        self.splitter.addWidget(self.file_list)

        left = QWidget(self.splitter)
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(theme.SPACE["xs"])
        self.grid = ThumbnailGrid(left, self)
        ll.addWidget(self.grid, 1)
        ll.addWidget(self._build_tile_bar(left))
        self.splitter.addWidget(left)

        right = QWidget(self.splitter)
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(theme.SPACE["xs"])
        rl.addWidget(self._build_anno_toolbar(right))
        self.pcanvas = PageCanvas(right, self.app)
        self.pcanvas.zoom_changed.connect(self._on_zoom_change)
        self.pcanvas.page_changed.connect(self._highlight_page)
        rl.addWidget(self.pcanvas, 1)
        self.splitter.addWidget(right)
        self.splitter.setCollapsible(1, False)
        self.splitter.setCollapsible(2, False)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setStretchFactor(2, 2)
        # Gitteret skal kunne vise mindst to fliser ved siden af hinanden --
        # ellers bliver det en enkelt lodret stribe, som er ubrugelig.
        left.setMinimumWidth(2 * self.grid.cell_w + 2 * PAD + 20)
        self.file_list.setMinimumWidth(120)
        self.file_list.setMaximumWidth(320)
        self.splitter.setSizes([self.FILE_LIST_W, 320, 700])

        self.grid.page_activated.connect(self.pcanvas.goto_page)
        self.grid.selection_changed.connect(self._report_selection)
        self.grid.context_page.connect(self._page_context_menu)
        self.grid.context_file.connect(self._file_context_menu)
        self.grid.external_drop.connect(self._on_external_drop)

        # Velkomsten ligger som et LAG over det hele -- ikke som en tredje
        # tilstand i splitteren. Panelernes bredder skal overleve, at man
        # tømmer kurven og fylder den igen, og et lag rører dem ikke.
        # Den tager ikke selv imod drop: ``MainWindow`` lytter paa vinduet, saa
        # et traek fra Stifinder falder igennem af sig selv.
        self._welcome_on = False
        self.welcome = welcome.WelcomeView(self)
        self.welcome.add_files_requested.connect(self.app._add)
        self.welcome.hide()

        # Vaerktoejet saettes foerst her: baren bygges foer fremviseren, og
        # ``_set_tool`` taler med begge.
        self._set_tool("hand")
        self._update_welcome()

    # ------------------------------------------------------------ velkomst
    def _update_welcome(self) -> None:
        """Vis velkomsten naar der ingen filer er -- og kun da.

        Tilstanden holdes i et flag og ikke i ``isVisible()``: et barn af et
        vindue der endnu ikke er vist, rapporterer *ikke* sig selv som synligt,
        saa opstarten ville ellers gaa i ring."""
        empty = not self.app.model.files
        if empty == self._welcome_on:
            return
        self._welcome_on = empty
        self.welcome.setGeometry(self.rect())
        self.welcome.setVisible(empty)
        if empty:
            self.welcome.raise_()

    def resizeEvent(self, event):  # noqa: N802 - Qt-API
        super().resizeEvent(event)
        # Uden betingelse: laget skal have den rigtige geometri, ogsaa foer
        # vinduet er vist foerste gang.
        self.welcome.setGeometry(self.rect())

    def _build_tile_bar(self, parent) -> QWidget:
        """Skyder til miniature-stoerrelsen, hoejrestillet som i Stifinder."""
        bar = QWidget(parent)
        row = QHBoxLayout(bar)
        row.setContentsMargins(theme.SPACE["sm"], 0, theme.SPACE["sm"],
                               theme.SPACE["xxs"])
        row.setSpacing(theme.SPACE["xs"])

        self._file_toggle = QToolButton(bar)
        self._file_toggle.setObjectName("ToolIcon")
        self._file_toggle.setCheckable(True)
        self._file_toggle.setChecked(True)
        self._file_toggle.setIcon(icons_vector.qicon("panel_left", theme.ICON["small"]))
        self._file_toggle.setIconSize(QSize(theme.ICON["small"], theme.ICON["small"]))
        self._file_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._file_toggle.toggled.connect(self.set_file_list_visible)
        Tooltip.attach(self._file_toggle, _("Vis eller skjul fillisten"))
        self._plain_icons.append((self._file_toggle, "panel_left", theme.ICON["small"], None))
        row.addWidget(self._file_toggle)

        # "Vaelg sider" staar ved siden af filliste-knappen: begge aendrer hvad
        # man SER paa fladen, ikke hvad der staar i dokumentet.
        self._select_toggle = QToolButton(bar)
        self._select_toggle.setObjectName("ToolIcon")
        self._select_toggle.setCheckable(True)
        self._select_toggle.setIcon(icons_vector.qicon("select_pages",
                                                       theme.ICON["small"]))
        self._select_toggle.setIconSize(QSize(theme.ICON["small"], theme.ICON["small"]))
        self._select_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._select_toggle.toggled.connect(self.grid.set_select_mode)
        # Slaar gitteret selv tilstanden til (Ctrl-klik, Ctrl+A), skal knappen
        # foelge med -- ellers viser den noget andet end fladen goer.
        self.grid.select_mode_changed.connect(self._select_toggle.setChecked)
        Tooltip.attach(self._select_toggle,
                       _("Vælg flere sider med flueben. Slås også til af Ctrl-klik."))
        self._plain_icons.append((self._select_toggle, "select_pages",
                                  theme.ICON["small"], None))
        row.addWidget(self._select_toggle)
        row.addStretch(1)

        small = QLabel(bar)
        small.setPixmap(icons_vector.qpixmap("zoom_out", theme.ICON["tiny"],
                                             theme.C["text_muted"]))
        row.addWidget(small)

        self._tile_slider = QSlider(Qt.Orientation.Horizontal, bar)
        self._tile_slider.setRange(TILE_SCALE_MIN, TILE_SCALE_MAX)
        self._tile_slider.setSingleStep(10)
        self._tile_slider.setPageStep(20)
        self._tile_slider.setValue(self.grid.tile_scale)
        self._tile_slider.setFixedWidth(120)
        self._tile_slider.setSizePolicy(QSizePolicy.Policy.Fixed,
                                        QSizePolicy.Policy.Fixed)
        Tooltip.attach(self._tile_slider,
                       lambda: _("Miniaturestørrelse: %d %%") % self.grid.tile_scale)
        self._tile_slider.valueChanged.connect(self.grid.set_tile_scale)
        row.addWidget(self._tile_slider)

        big = QLabel(bar)
        big.setPixmap(icons_vector.qpixmap("zoom_in", theme.ICON["small"],
                                           theme.C["text_muted"]))
        row.addWidget(big)
        self._tile_zoom_labels = ((small, "zoom_out", theme.ICON["tiny"]),
                                  (big, "zoom_in", theme.ICON["small"]))
        return bar

    FILE_LIST_W = 190

    def set_file_list_visible(self, on: bool) -> None:
        """Klap fillisten sammen eller ud.

        Bredden tages fra (og gives tilbage til) gitteret ved siden af, saa
        fremviseren til hoejre ikke flytter sig."""
        files, grid, viewer = self.splitter.sizes()
        if on:
            if files < 40:
                self.splitter.setSizes(
                    [self.FILE_LIST_W, max(160, grid - self.FILE_LIST_W), viewer])
        else:
            self.splitter.setSizes([0, files + grid, viewer])
        if self._file_toggle.isChecked() != on:
            self._file_toggle.setChecked(on)

    def tile_scale(self) -> int:
        return self.grid.tile_scale

    def set_tile_scale(self, percent) -> None:
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            return
        self._tile_slider.setValue(percent)     # valueChanged driver gitteret

    def _on_files_reordered(self, payload) -> None:
        """Slip i fillisten: flyt filen (med sine udtrukne boern) til den plads.

        Boernene klaeber til deres moder i ``sort_order()``, saa en flytning der
        efterlod dem ville se ud som om de sprang tilbage. Derfor flyttes hele
        blokken -- praecis som "Flyt øverst"/"Flyt nederst" gør."""
        iid, row = payload[0], int(payload[1])
        model = self.app.model
        if model.entry_by_iid(iid) is None:
            return
        order = [f.iid for f in model.files]
        block = [iid] + [f.iid for f in model.files if f.origin_iid == iid]
        # Indsaettelsespladsen regnes i den liste brugeren SER. ``anchor`` er
        # den fil der skal ende EFTER blokken; efter at blokken er taget ud,
        # findes pladsen ved at slaa ankeret op i resten.
        anchor = order[row] if 0 <= row < len(order) else None
        if anchor is not None and anchor in set(block):
            return          # sluppet paa sig selv -- ingen flytning
        rest = [i for i in order if i not in set(block)]
        at = rest.index(anchor) if anchor in rest else len(rest)
        new_order = rest[:at] + block + rest[at:]
        if new_order == order:
            return
        self.app.undo_stack.push(em.reorder_files_cmd(model, new_order))
        self.rebuild()
        self.grid.select_file(iid)
        self.app.after_model_change()

    def _on_file_chosen(self, iid: str) -> None:
        """Klik i fillisten: markér filen og rul hen til dens hoved."""
        self.grid.select_file(iid)
        self.grid.ensure_visible("h:" + iid)
        self.grid.viewport().update()

    def refresh_themed_icons(self) -> None:
        """Gentegn vaerktoejslinjens ikoner efter et lys/moerk-skift."""
        size = theme.ICON["tool"]
        for btn, name, sz, color_key in list(self._plain_icons):
            try:
                btn.setIcon(icons_vector.qicon(
                    name, sz, theme.C[color_key] if color_key else None))
            except RuntimeError:
                pass
        for name, btn in self._tool_buttons.items():
            try:
                btn.setIcon(icons_vector.qicon(
                    self._tool_icons[name], size,
                    self._tool_color(name, name == self.active_tool)))
            except RuntimeError:
                pass
        try:
            self._color_btn.setIcon(icons_vector.swatch_qicon(
                self._anno_color_hex(), size))
        except RuntimeError:
            pass
        for label, name, sz in getattr(self, "_tile_zoom_labels", ()):
            try:
                label.setPixmap(icons_vector.qpixmap(name, sz, theme.C["text_muted"]))
            except RuntimeError:
                pass
        self.file_list.rebuild()
        self.file_list.select_file(self.grid.selected_file())
        self.grid.viewport().update()
        self.pcanvas.viewport().update()
        self.welcome.refresh_theme()

    # ---------------------------------------------------------- delegering
    def rebuild(self) -> None:
        self.grid.rebuild()
        self.file_list.rebuild()
        self.file_list.select_file(self.grid.selected_file())
        self.pcanvas.set_document()
        self._update_welcome()

    def refresh_header(self, iid) -> None:
        """Hovedet males af gitteret, saa en gentegning er alt der skal til.
        Fillisten viser samme navn/haengelaas og opdateres med."""
        self.grid.viewport().update()
        self.file_list.rebuild()
        self.file_list.select_file(self.grid.selected_file())

    def selected_uids(self) -> list:
        return self.grid.selected_uids()

    def selected_file(self):
        return self.grid.selected_file()

    def selected_page_uid(self):
        return self.grid.selected_page_uid()

    def page_number_label(self, uid):
        return self.grid.page_number_label(uid)

    def select_and_reveal(self, uid) -> None:
        self.grid.select_and_reveal(uid)

    def reselect(self, uids) -> None:
        self.grid.reselect(uids)

    def cancel_drag(self) -> None:
        self.grid.cancel_drag()

    def set_drop_highlight(self, on: bool) -> None:
        self.grid.set_drop_highlight(on)
        self.welcome.set_drop_highlight(on)

    def drop_file_index(self, global_pos):
        return self.grid.drop_file_index(global_pos)

    def setFocus(self):  # noqa: N802 - Qt-API
        self.grid.setFocus()

    def _on_external_drop(self, paths, index) -> None:
        """Filer slupket fra Stifinder -- baade paa gitteret og paa fillisten."""
        supported = {".pdf", ".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}
        keep = [p for p in paths
                if p and os.path.exists(p) and Path(p).suffix.lower() in supported]
        if not keep:
            return
        added = []
        at = "end" if index is None else index
        for p in keep:
            entry = self.app._add_file_entry(p, index=at)
            if entry is not None:
                added.append(entry)
                if at != "end":
                    at += 1
        self.app._mark_for_unlock_prompt(added)
        self.app._load_metadata_async(added)
        self.app._after_files_added()

    # ------------------------------------------------------------ status
    def _report_selection(self) -> None:
        iid = self.grid.selected_file()
        self.file_list.select_file(iid)
        if iid:
            entry = self.app.model.entry_by_iid(iid)
            if entry is not None:
                self.app.set_status(_("Fil valgt: %s") % Path(entry.path).name)
            return
        sel = self.grid.selected_uids()
        if len(sel) > 1:
            self.app.set_status(_("%(s)d sider valgt") % {"s": len(sel)})
        elif sel:
            idx = self.grid.page_order.index(sel[0]) + 1
            self.app.set_status(_("Side %(i)d af %(n)d")
                                % {"i": idx, "n": len(self.grid.page_order)})
        else:
            self.app.set_status()

    def _highlight_page(self, uid) -> None:
        """Kaldes naar fremviseren scroller til en ny side.

        Den maa IKKE flytte markeringen: ``goto_page`` klamper ved dokumentets
        slutning, saa den viste side kan vaere en SENERE end den man netop
        pilede hen til -- og markeringen ville hoppe foran. Her opdateres kun
        statuslinjen."""
        try:
            idx = self.grid.page_order.index(uid) + 1
        except ValueError:
            return
        if self.grid.selected_uids() or self.grid.selected_file():
            return
        self.app.set_status(_("Side %(i)d af %(n)d")
                            % {"i": idx, "n": len(self.grid.page_order)})

    # --------------------------------------------------------- kontekstmenu
    def _page_context_menu(self, uid: str, global_pos) -> None:
        menu = QMenu(self)
        menu.addAction(_("Roter venstre"), lambda: self._rotate_ctx(uid, -90))
        menu.addAction(_("Roter højre"), lambda: self._rotate_ctx(uid, 90))
        menu.addSeparator()
        targets = self._ctx_targets(uid)
        export = menu.addMenu(_("Eksportér side") if len(targets) == 1
                              else _("Eksportér %d sider") % len(targets))
        for fmt, label in (("pdf", "PDF"), ("md", "Markdown"), ("epub", "ePub"),
                           ("jpg", "JPG"), ("png", "PNG")):
            export.addAction(label,
                             lambda f=fmt, t=tuple(targets): self.app.export_pages(list(t), f))
        menu.addAction(_("Kopiér til udklipsholder…"),
                       lambda t=tuple(targets): self.app.copy_to_clipboard(list(t)))
        menu.addSeparator()
        menu.addAction(_("Slet side"), lambda: self.delete_pages(self._ctx_targets(uid)))
        menu.exec(global_pos)

    def _file_context_menu(self, iid: str, global_pos) -> None:
        entry = self.app.model.entry_by_iid(iid)
        menu = QMenu(self)
        unlock = menu.addAction(_("Lås op"), lambda: self.app.unlock_file(iid))
        unlock.setEnabled(entry is not None and entry.enc_key == pdf_utils.ENC_ENCRYPTED)
        menu.addAction(_("Vælg alle sider i filen"),
                       lambda: self.grid.select_all_in_file(iid))
        menu.addAction(_("Kopiér til udklipsholder…"),
                       lambda: self.app.copy_to_clipboard(
                           [p.uid for p in (entry.pages if entry else ())]))
        menu.addSeparator()
        menu.addAction(_("Slet fil"), lambda: self.app.delete_file(iid))
        menu.exec(global_pos)

    def _ctx_targets(self, uid: str) -> list:
        """Klikkede man inde i en flermarkering, gaelder handlingen hele
        markeringen; ellers kun den flise man ramte."""
        sel = self.grid.selected_uids()
        return sel if uid in sel else [uid]

    def _rotate_ctx(self, uid, delta) -> None:
        self.rotate_pages(self._ctx_targets(uid), delta)

    def delete_selected_key(self) -> None:
        sel = self.grid.selected_uids()
        if sel:
            self.delete_pages(sel)
        elif self.grid.selected_file():
            self.app.delete_file(self.grid.selected_file())

    # ------------------------------------------------------------ mutation
    def rotate_selected(self, delta) -> None:
        """Kommandobarens roter-knapper. Gaar gennem :meth:`rotate_pages`, IKKE
        gennem fremviseren: den ville kun genopbygge den store visning, saa
        miniaturen beholdt sit gamle billede."""
        uids = self.grid.selected_uids()
        if uids:
            self.rotate_pages(uids, delta)

    def rotate_pages(self, uids, delta) -> None:
        """Roter sider som ÉN undo-handling og opdater fliserne straks.

        Rotation er ikke en render-invalidering: den cachede miniature roteres
        paa stedet (sub-ms) og gemmes under den nye noegle, saa der er nul
        ``PDF_LOCK``-trafik."""
        uids = [u for u in uids if self.app.model.page_by_uid(u)]
        if not uids:
            return
        pages = {u: self.app.model.page_by_uid(u)[1] for u in uids}
        cached = {u: self.render_mgr.cache.get(self.grid.tile_key(p))
                  for u, p in pages.items()}
        self.app.undo_stack.push(em.rotate_pages_cmd(self.app.model, uids, delta))
        for uid in uids:
            self._retile_after_rotate(uid, pages[uid], cached.get(uid), delta)
        self.pcanvas.set_document()

    def _retile_after_rotate(self, uid, page, cached, delta) -> None:
        """Genbrug den allerede renderede miniature i stedet for at rendere igen."""
        if cached is not None:
            try:
                rotated = cached.rotate(-delta, expand=True)
                rotated.thumbnail(self.grid.tile_img, Image.Resampling.LANCZOS)
                self.render_mgr.cache.put(self.grid.tile_key(page), rotated)
                self.grid._set_tile_image(uid, rotated)
                return
            except Exception as e:
                logger.debug("Kunne ikke rotere cachet miniature: %s", e)
        self.grid._pix_key.pop(uid, None)
        self.grid._request_visible_renders()

    def delete_pages(self, uids) -> None:
        """Haard fjernelse fra modellen (kildefilerne roeres ikke) som ÉN
        undo-handling. Toemmes en fil helt, fjernes hele ``FileEntry``."""
        uids = [u for u in uids if self.app.model.page_by_uid(u)]
        if not uids:
            return
        # Efter sletningen markeres naboen: foerste overlevende EFTER den sidst
        # slettede side, ellers den sidste overlevende foer den.
        order = self.grid.page_order
        doomed = set(uids)
        last = max((i for i, u in enumerate(order) if u in doomed), default=-1)
        nxt = next((u for u in order[last + 1:] if u not in doomed), None)
        if nxt is None:
            nxt = next((u for u in reversed(order[:last]) if u not in doomed), None)
        self.app.undo_stack.push(em.delete_pages_cmd(self.app.model, uids))
        self.rebuild()
        if nxt:
            self.grid.select_and_reveal(nxt)
        self.app.after_model_change()

    def commit_page_drop(self, uids, target) -> None:
        """Afslut et sidetraek: flyt ind i en fil, eller riv ud som egen fil."""
        uids = [u for u in uids if self.app.model.page_by_uid(u)]
        if not target or not uids:
            return
        if target[0] == "into":
            _k, dst_iid, visual = target
            entry = self.app.model.entry_by_iid(dst_iid)
            if entry is None:
                return
            pos = self._drop_position(entry, uids, visual)
            # Slippes udsnittet praecis der hvor det allerede ligger, er der
            # intet at fortryde -- undgaa et tomt undo-trin.
            want = set(uids)
            cur = [pg.uid for pg in entry.pages]
            rest = [u for u in cur if u not in want]
            if rest[:pos] + uids + rest[pos:] == cur:
                return
            self.app.undo_stack.push(em.move_pages_cmd(self.app.model, uids, dst_iid, pos))
        else:
            self.app.undo_stack.push(
                em.extract_pages_cmd(self.app.model, uids, at_index=target[1]))
        self.rebuild()
        self.grid.reselect(uids)
        self.app.after_model_change()

    @staticmethod
    def _drop_position(entry, uids, visual) -> int:
        """Visuel indsaetningsplads -> plads EFTER at de trukne sider er taget ud.

        Drop-maalet regnes i den liste brugeren SER, hvor de trukne sider stadig
        er med. ``move_pages`` indsaetter derimod i listen efter at de er
        fjernet. Traekker man fremad inde i samme fil, skal pladsen derfor
        reduceres med antallet af trukne sider der laa foer den."""
        want = set(uids)
        before = sum(1 for pg in entry.pages[:visual] if pg.uid in want)
        return max(0, visual - before)

    # ------------------------------------------------- annotationslinjen
    def _build_anno_toolbar(self, parent) -> QWidget:
        """Vaerktoejslinje til annotation/maskering + zoom.

        Grupperet med smaa overskrifter (Zoom · Vælg · Marker · Masker)
        adskilt af separatorer. Rene ikonknapper med tooltip; kun det
        valgte vaerktoejs ikon farves (accent, eller roedt for maskering).
        """
        bar = QWidget(parent)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(theme.SPACE["xs"])

        def group(title: str) -> QHBoxLayout:
            box = QWidget(bar)
            bl = QVBoxLayout(box)
            bl.setContentsMargins(0, 0, 0, 0)
            bl.setSpacing(0)
            cap = QLabel(title, box)
            cap.setObjectName("Muted")
            cap.setFont(theme.font("caption"))
            cap.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            bl.addWidget(cap)
            row = QWidget(box)
            rl = QHBoxLayout(row)
            rl.setContentsMargins(0, 0, 0, 0)
            rl.setSpacing(theme.SPACE["xxs"])
            bl.addWidget(row)
            lay.addWidget(box)
            return rl

        def sep() -> None:
            line = theme.hairline("vertical", bar)
            line.setFixedHeight(34)
            lay.addSpacing(theme.SPACE["xs"])
            lay.addWidget(line)
            lay.addSpacing(theme.SPACE["xs"])

        def icon_btn(row, icon, tip, slot, color_key=None) -> QToolButton:
            # Farven gemmes som TOKENNAVN, ikke som vaerdi: efter et temaskift
            # skal ikonet gentegnes i den NYE tone af samme token.
            btn = QToolButton(bar)
            btn.setObjectName("ToolIcon")
            size = theme.ICON["tool"]
            color = theme.C[color_key] if color_key else None
            btn.setIcon(icons_vector.qicon(icon, size, color))
            btn.setIconSize(QSize(size, size))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(slot)
            Tooltip.attach(btn, tip)
            row.addWidget(btn)
            # Ikonet er tegnet i en tokenfarve og skal gentegnes ved temaskift.
            self._plain_icons.append((btn, icon, size, color_key))
            return btn

        def tool_btn(row, tool, iconname, label) -> None:
            self._tool_icons[tool] = iconname
            btn = QToolButton(bar)
            btn.setObjectName("ToolIcon")
            btn.setCheckable(True)
            size = theme.ICON["tool"]
            btn.setIcon(icons_vector.qicon(iconname, size, self._tool_color(tool, False)))
            btn.setIconSize(QSize(size, size))
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _c=False, t=tool: self._set_tool(t))
            Tooltip.attach(btn, label)
            row.addWidget(btn)
            self._tool_buttons[tool] = btn

        # --- Zoom (som i en PDF-fremviser) ---
        zr = group(_("Zoom"))
        icon_btn(zr, "zoom_out", _("Zoom ud"), lambda: self.pcanvas.zoom_out())
        self._zoom_entry = QLineEdit("100%", bar)
        self._zoom_entry.setFixedWidth(58)
        self._zoom_entry.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_entry.editingFinished.connect(self._apply_zoom_entry)
        zr.addWidget(self._zoom_entry)
        icon_btn(zr, "zoom_in", _("Zoom ind"), lambda: self.pcanvas.zoom_in())
        sep()

        # --- Vælg (navigation/markering) ---
        vr = group(_("Vælg"))
        tool_btn(vr, "hand", "tool_hand", _("Flyt"))
        tool_btn(vr, "select", "tool_select", _("Marker"))
        sep()

        # --- Marker (farve + tegne- og tekstvaerktoejer). Farven staar
        #     forrest: den gaelder alt i gruppen (og den valgte figur). ---
        mr = group(_("Marker"))
        self._color_btn = QToolButton(bar)
        self._color_btn.setObjectName("ToolIcon")
        self._color_btn.setIcon(icons_vector.swatch_qicon(self._anno_color_hex(),
                                                          theme.ICON["tool"]))
        self._color_btn.setIconSize(QSize(theme.ICON["tool"], theme.ICON["tool"]))
        self._color_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_btn.clicked.connect(self._pick_color)
        Tooltip.attach(self._color_btn, _("Vælg farve"))
        mr.addWidget(self._color_btn)
        mr.addSpacing(theme.SPACE["xs"])
        tool_btn(mr, "rect", "tool_rect", _("Boks"))
        tool_btn(mr, "circle", "tool_circle", _("Cirkel"))
        tool_btn(mr, "line", "tool_line", _("Streg"))
        tool_btn(mr, "ink", "tool_ink", _("Frihånd"))
        tool_btn(mr, "freetext", "tool_freetext", _("Tekst"))
        tool_btn(mr, "highlight", "tool_highlight", _("Fremhæv"))
        tool_btn(mr, "underline", "tool_underline", _("Understreg"))
        tool_btn(mr, "strikeout", "tool_strike", _("Gennemstreg"))
        sep()

        # --- Masker (redaction) + soegning ---
        kr = group(_("Masker"))
        tool_btn(kr, "redact", "tool_redact", _("Masker boks"))
        tool_btn(kr, "redact_text", "tool_redact_text", _("Masker ord"))
        self._search_entry = QLineEdit(bar)
        self._search_entry.setFixedWidth(150)
        self._search_entry.setPlaceholderText(_("Søg…"))
        self._search_entry.returnPressed.connect(self._run_search_redaction)
        kr.addWidget(self._search_entry)
        icon_btn(kr, "search_redact", _("Søg og masker"), self._run_search_redaction,
                 color_key="danger")
        self._search_status = QLabel("", bar)
        self._search_status.setObjectName("Muted")
        kr.addWidget(self._search_status)

        lay.addStretch(1)
        return bar

    @staticmethod
    def _tool_color(tool: str, selected: bool) -> str:
        """Ikonfarve for et vaerktoej. Maskeringsvaerktoejer er altid roede
        (destruktivt); oevrige er neutrale i hvile og accent naar valgt."""
        if tool in _REDACT_TOOLS:
            return theme.C["danger"]
        return theme.C["accent"] if selected else theme.C["text"]

    def _set_tool(self, tool: str) -> None:
        self.active_tool = tool
        size = theme.ICON["tool"]
        for name, btn in self._tool_buttons.items():
            btn.setChecked(name == tool)
            btn.setIcon(icons_vector.qicon(self._tool_icons[name], size,
                                           self._tool_color(name, name == tool)))
        self.pcanvas.set_tool(tool)
        self.app.set_crop_active(tool == "crop")

    def activate_crop_tool(self) -> None:
        """Kommandobarens Beskær: slaa beskaerings-vaerktoejet til i fremviseren."""
        if self.active_tool == "crop":
            self._set_tool("hand")
            return
        self.active_tool = "crop"
        for name, btn in self._tool_buttons.items():
            btn.setChecked(False)
        self.pcanvas.set_tool("crop")
        self.app.set_crop_active(True)
        self.app.set_status(_("Træk en ramme på siden for at beskære den"))

    def _anno_color_hex(self) -> str:
        return "#%02x%02x%02x" % tuple(int(round(c * 255)) for c in self._anno_color)

    def _pick_color(self) -> None:
        col = QColorDialog.getColor(QColor(self._anno_color_hex()), self,
                                    _("Vælg farve"))
        if not col.isValid():
            return
        self._anno_color = (col.redF(), col.greenF(), col.blueF())
        # Farven bruges til (a) NAESTE nye tegning og (b) den aktuelt valgte
        # annotation, hvis en er valgt -- saa farveknappen "virker" baade for
        # kommende og allerede tegnede figurer.
        self.pcanvas.set_color(self._anno_color)
        self.pcanvas.recolor_selection(self._anno_color)
        self._color_btn.setIcon(icons_vector.swatch_qicon(self._anno_color_hex(),
                                                          theme.ICON["tool"]))

    # ------------------------------------------------------------ zoom
    def _on_zoom_change(self, pct: int) -> None:
        self._zoom_entry.setText("%d%%" % pct)

    def _apply_zoom_entry(self) -> None:
        self.pcanvas.set_zoom_percent(self._zoom_entry.text().strip().rstrip("%").strip())

    # ------------------------------------------------- søg og masker
    def search_and_redact(self, text: str) -> None:
        """Kør en soege-maskering paa ``text`` (fremviserens hoejreklik-menu).

        Feltet ryddes bagefter: soegningen er udfoert, og en efterladt streng ser
        ud som om der stadig er noget at soege efter."""
        self._search_entry.setText(text)
        self._run_search_redaction()
        self._search_entry.clear()

    # ------------------------------------------------- tekstgenkendelse (OCR)
    def run_ocr(self) -> None:
        """Kør tekstgenkendelse på de sider der ikke har et tekstlag.

        Kaldes fra **kommandobaren** (knappen ligger til venstre for Anonymiser);
        derfor er den offentlig.

        Fremviseren gør det **ikke** af sig selv. En stille OCR af "den side man
        står på" tog sekunder uden at brugeren kunne se hvorfor markeringen
        virkede ét sted og ikke et andet; her er det en handling man beder om, med
        fremdrift og et resultat der kan aflæses.

        Grupperingen sker efter ``page.src_path`` -- ikke ``entry.path`` -- fordi
        en fils sideliste kan indeholde sider fra en anden PDF ("Indsæt side").
        """
        if not ocr_text.ocr_available():
            qt_util.warn(self.app, _("Tekstgenkendelse"),
                         _("Tekstgenkendelse er ikke tilgængelig i denne "
                           "installation."))
            return

        jobs = {}
        for entry in self.app.model.files:
            for p in entry.pages:
                is_pdf = em.kind_for_path(p.src_path) == em.KIND_PDF
                idx = p.src_index if is_pdf else 0
                key = (os.path.normcase(p.src_path), idx)
                job = jobs.get(key)
                if job is None:
                    # Brugerens egen rotation er det bedste gæt på hvilken vej en
                    # scanning vender; orienteringsprøven starter dér.
                    jobs[key] = {"path": p.src_path, "index": idx,
                                 "is_pdf": is_pdf, "rotation": p.rotation,
                                 "uids": [p.uid]}
                else:
                    job["uids"].append(p.uid)

        if not jobs:
            self.app.set_status(_("Tilføj først en eller flere filer."),
                                transient_ms=5000, kind="warning")
            return

        total = len(jobs)
        prog = qt_util.ProgressDialog(
            self.app, _("Tekstgenkendelse"),
            _("Læser teksten på de scannede sider…"), cancellable=True)
        prog.set_fraction(0, total)
        # Ikke "Initialiserer..." et sekund: sig med det samme hvad der sker.
        prog.set_status(_("Gennemgår sider…"))
        prog.show()
        qt_util.run_in_thread(self._ocr_worker, list(jobs.values()),
                              self.app._get_all_passwords(), prog, total,
                              name="viewer-ocr")

    def _ocr_worker(self, jobs, pw, prog, total) -> None:
        by_uid = {}
        scanned = 0
        last = [0.0]

        def say(done, job, ocring) -> None:
            """Sig hvad der sker LIGE NU -- fil, side og om der OCR'es.

            Uden det stod dialogen med "Initialiserer..." koerslen igennem, og
            en OCR af et bundt tager minutter. Struber som anonymiseringen, saa
            en fil med tekstlag ikke drukner UI'en i signaler.
            """
            now = time.monotonic()
            if done and now - last[0] < 0.12:
                return
            last[0] = now
            label = _("%(fil)s · side %(nr)d") % {
                "fil": os.path.basename(job["path"]), "nr": job["index"] + 1}
            head = (_("Læser teksten med tekstgenkendelse…") if ocring
                    else _("Gennemgår sider…"))
            self.app._queue.put((prog.set_status, (
                "%s  ·  %s" % (head, _("Side %(nr)d af %(alle)d · %(navn)s")
                               % {"nr": done + 1, "alle": total, "navn": label}),)))

        for n, job in enumerate(jobs):
            if prog.cancel_event is not None and prog.cancel_event.is_set():
                break
            try:
                if job["is_pdf"]:
                    say(n, job, False)
                    native = pdf_renderer.page_words(job["path"], pw, job["index"])
                    if sum(len(w[4].strip()) for w in native) >= ocr_text.MIN_NATIVE_CHARS:
                        words = native          # tekstlag: OCR ville være spild
                    else:
                        # Den langsomme gren: sig det, og sig det ustrubet --
                        # her gaar der sekunder foer naeste opdatering.
                        last[0] = 0.0
                        say(n, job, True)
                        words = redaction.words_for_page(
                            job["path"], pw, job["index"],
                            rotation_hint=job["rotation"],
                            cancel=prog.cancel_event)
                        scanned += 1
                else:
                    last[0] = 0.0
                    say(n, job, True)
                    words = ocr_text.image_words_a_space(
                        job["path"], rotation_hint=job["rotation"],
                        cancel=prog.cancel_event)
                    scanned += 1
            except Exception as e:
                logger.info("Tekstgenkendelse af %s side %s fejlede: %s",
                            job["path"], job["index"], e)
                words = []
            if words:
                for uid in job["uids"]:
                    by_uid[uid] = words
            self.app._queue.put((prog.set_fraction, (n + 1, total)))
        self.app._queue.put((self._apply_ocr, (by_uid, scanned, prog)))

    def _apply_ocr(self, by_uid, scanned, prog=None) -> None:
        if prog is not None:
            prog.finish()
        self.pcanvas.apply_ocr_words(by_uid)
        readable = sum(1 for words in by_uid.values() if words)
        if not readable:
            self.app.set_status(_("Der blev ikke fundet tekst at markere"),
                                transient_ms=8000, kind="warning")
            return
        if not scanned:
            self.app.set_status(
                _("Ingen scannede sider — teksten kunne markeres i forvejen"),
                transient_ms=8000)
            return
        self.app.set_status(
            _("Tekstgenkendelse færdig — teksten kan nu markeres på %d sider")
            % readable, transient_ms=8000, kind="success")

    def _run_search_redaction(self) -> None:
        """Soeg og masker paa tvaers af **alle** aabne filer.

        Grupperingen sker efter ``page.src_path`` -- ikke ``entry.path`` -- fordi
        en fils sideliste kan indeholde sider fra en anden PDF ("Indsaet side").
        Hver kildefil aabnes dermed praecis én gang, og kun de sider der faktisk
        ligger i modellen scannes.

        Billedfiler er **med**: de blev tidligere sprunget over, saa en indsat
        scanning i .jpg/.png aldrig kunne maskeres via soegning. De faar samme
        OCR-behandling som scannede PDF-sider.
        """
        query = self._search_entry.text().strip()
        if not query:
            return

        pdf_jobs = {}
        image_jobs = {}
        for entry in self.app.model.files:
            for p in entry.pages:
                key = os.path.normcase(p.src_path)
                if em.kind_for_path(p.src_path) == em.KIND_PDF:
                    _path, idxs, rots = pdf_jobs.setdefault(
                        key, (p.src_path, set(), {}))
                    idxs.add(p.src_index)
                    # Brugerens egen rotation er det bedste gaet paa hvilken vej
                    # en scannet side vender; orienteringsproeven starter dér.
                    if p.rotation:
                        rots[p.src_index] = p.rotation
                else:
                    image_jobs.setdefault(key, (p.src_path, p.rotation))

        if not pdf_jobs and not image_jobs:
            self._search_status.setText(_("Ingen forekomster"))
            return

        total = sum(len(idxs) for _p, idxs, _r in pdf_jobs.values()) + len(image_jobs)
        self._search_status.setText(_("Søger…"))
        # OCR-soegning kan tage minutter paa et scannet bundt, saa den tavse
        # worker fra foer er erstattet af en afbrydelig fremdriftsdialog.
        prog = qt_util.ProgressDialog(
            self.app, _("Søg og masker"),
            _("Søger efter \"%s\"…") % query, cancellable=True)
        prog.set_fraction(0, total)
        prog.show()
        qt_util.run_in_thread(
            self._search_worker,
            [(path, sorted(idxs), rots) for path, idxs, rots in pdf_jobs.values()],
            list(image_jobs.values()),
            self.app._get_all_passwords(), query, prog, total,
            name="search-redact")

    def _search_worker(self, jobs, images, pw, query, prog, total) -> None:
        results = {}
        done = [0]

        def bump(_n=None) -> None:
            done[0] += 1
            self.app._queue.put((prog.set_fraction, (done[0], total)))

        try:
            for path, indices, rots in jobs:
                if prog.cancel_event is not None and prog.cancel_event.is_set():
                    break
                hits = redaction.scan_indices(
                    path, pw, query, indices, rotations=rots,
                    on_page=lambda _n: bump(), cancel=prog.cancel_event)
                if hits:
                    results[os.path.normcase(path)] = hits
            for path, rotation in images:
                if prog.cancel_event is not None and prog.cancel_event.is_set():
                    break
                rects = redaction.scan_image(path, query, rotation_hint=rotation,
                                             cancel=prog.cancel_event)
                bump()
                if rects:
                    # Billeder har kun én "side"; index 0 holder formen ens.
                    results[os.path.normcase(path)] = {0: rects}
        except Exception as e:
            logger.exception("Soegningen fejlede: %s", e)
        self.app._queue.put((self._apply_search_redaction, (query, results, prog)))

    def _apply_search_redaction(self, query, results, prog=None) -> None:
        if prog is not None:
            prog.finish()
        items = []
        if results:
            for entry in self.app.model.files:
                for p in entry.pages:
                    is_pdf = em.kind_for_path(p.src_path) == em.KIND_PDF
                    idx = p.src_index if is_pdf else 0
                    rects = results.get(os.path.normcase(p.src_path), {}).get(idx)
                    if not rects:
                        continue
                    items.append((p.uid, em.AnnotationSpec(
                        kind=an.ANNOT_REDACT, rects=tuple(rects), color=(0, 0, 0),
                        fill=(0, 0, 0), source="search", label=query)))
        if not items:
            self._search_status.setText(_("Ingen forekomster"))
            return
        self.app.undo_stack.push(em.add_annotations_batch_cmd(self.app.model, items))
        self.pcanvas.redraw_overlays()
        total = sum(len(spec.rects) for _uid, spec in items)
        self._search_status.setText(_("%s forekomster markeret") % total)
