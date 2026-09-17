"""Vektorikoner tegnet i kode med PIL, leveret som ``QIcon``.

Hvorfor ikke PNG'er eller SVG-filer: hvert ikon tegnes her som streger i et
normaliseret 0..1-rum, rasteres i **4x supersample** og skaleres ned med
LANCZOS. Det giver et skarpt ikon i enhver stoerrelse og enhver farve, uden en
eneste billedfil i repoet -- og uden en SVG-renderer i Nuitka-builden.

Konventioner (holdes ens over hele saettet, ellers ser baren rodet ud):
  * alt tegnes inden for 10 % margin
  * stregbredde ``w = max(1, round(S * 0.085))`` af den supersamplede kant
  * runde streg-ender simuleres med en ``w``-diameter cirkel i hvert endepunkt

**Ingen ``ImageFont.truetype``** noget sted -- bogstav-glyffer (A, T) tegnes med
linjer. Det holder Nuitka-builden fri af ``PIL._imagingft``.

Qt-integration: ``qicon(navn)`` returnerer en cachet ``QIcon`` med baade en
normal og en *disabled* tone lagt ind, saa Qt selv nedtoner ikonet naar knappen
slaas fra. 8.x' ``IconFactory``/``set_button_state``-dans om ``ImageTk``-
referencer og manuel graatoning er dermed vaek: ``QIcon`` ejer sine pixmaps.

Ikonet tegnes i ``size`` logiske px, men rasteres i skaermens *device*-pixels og
faar ``devicePixelRatio`` sat, saa det er skarpt paa 125-200 %-skaerme.
"""

from __future__ import annotations

import math

from PIL import Image, ImageDraw
from PySide6.QtGui import QIcon, QImage, QPixmap

from . import theme
from .logging_config import get_logger

logger = get_logger(__name__)

SS = 4                 # supersample-faktor
MARGIN = 0.10          # normaliseret margin
STROKE = 0.085         # stregbredde som andel af kantlaengden

_REGISTRY: dict[str, callable] = {}
_PIL_CACHE: dict[tuple, Image.Image] = {}
_QICON_CACHE: dict[tuple, QIcon] = {}
_warned_missing: set[str] = set()


def icon(name: str):
    """Registrér en tegnefunktion under ``name``."""
    def deco(fn):
        _REGISTRY[name] = fn
        return fn
    return deco


def has_icon(name: str) -> bool:
    return name in _REGISTRY


def icon_names() -> list[str]:
    return sorted(_REGISTRY)

# --------------------------------------------------------------------------
# Pen: normaliseret tegne-API (0..1 i begge akser)
# --------------------------------------------------------------------------
class _Pen:
    def __init__(self, draw: ImageDraw.ImageDraw, size: int, color, width: int):
        self.d = draw
        self.S = size
        self.c = color
        self.w = width

    # -- koordinathjaelpere
    def _x(self, v: float) -> float:
        return v * self.S

    def _pt(self, p) -> tuple[float, float]:
        return (p[0] * self.S, p[1] * self.S)

    def _cap(self, p) -> None:
        """Rund streg-ende: en cirkel med stregbreddens diameter."""
        x, y = self._pt(p)
        r = self.w / 2.0
        self.d.ellipse((x - r, y - r, x + r, y + r), fill=self.c)

    # -- primitiver
    def line(self, *points, caps: bool = True, w: float | None = None) -> None:
        width = self.w if w is None else max(1, int(round(w * self.S)))
        pts = [self._pt(p) for p in points]
        self.d.line(pts, fill=self.c, width=width, joint="curve")
        if caps:
            saved, self.w = self.w, width
            for p in points:
                self._cap(p)
            self.w = saved

    def rect(self, x0, y0, x1, y1, r: float = 0.0) -> None:
        box = (self._x(x0), self._x(y0), self._x(x1), self._x(y1))
        if r > 0:
            self.d.rounded_rectangle(box, radius=self._x(r),
                                     outline=self.c, width=self.w)
        else:
            self.d.rectangle(box, outline=self.c, width=self.w)

    def frect(self, x0, y0, x1, y1, r: float = 0.0) -> None:
        box = (self._x(x0), self._x(y0), self._x(x1), self._x(y1))
        if r > 0:
            self.d.rounded_rectangle(box, radius=self._x(r), fill=self.c)
        else:
            self.d.rectangle(box, fill=self.c)

    def circle(self, cx, cy, r) -> None:
        box = (self._x(cx - r), self._x(cy - r), self._x(cx + r), self._x(cy + r))
        self.d.ellipse(box, outline=self.c, width=self.w)

    def fcircle(self, cx, cy, r) -> None:
        box = (self._x(cx - r), self._x(cy - r), self._x(cx + r), self._x(cy + r))
        self.d.ellipse(box, fill=self.c)

    def arc(self, cx, cy, r, start, end) -> None:
        box = (self._x(cx - r), self._x(cy - r), self._x(cx + r), self._x(cy + r))
        self.d.arc(box, start, end, fill=self.c, width=self.w)

    def poly(self, points) -> None:
        self.d.polygon([self._pt(p) for p in points], fill=self.c)

    def arrow_head(self, tip, angle_deg: float, size: float = 0.22) -> None:
        """Udfyldt trekantet pilespids i ``tip``, pegende mod ``angle_deg``.

        0 grader = mod hoejre, positiv retning er med uret (skaermkoordinater).
        """
        a = math.radians(angle_deg)
        bx = tip[0] - math.cos(a) * size
        by = tip[1] - math.sin(a) * size
        nx, ny = -math.sin(a), math.cos(a)
        half = size * 0.55
        self.poly([tip,
                   (bx + nx * half, by + ny * half),
                   (bx - nx * half, by - ny * half)])


# --------------------------------------------------------------------------
# Delte del-glyffer
# --------------------------------------------------------------------------
def _page(p: _Pen, x0=0.20, y0=0.10, x1=0.72, y1=0.90, fold=0.18) -> None:
    """Dokument med aeselore i oeverste hoejre hjoerne."""
    p.line((x0, y0), (x1 - fold, y0), (x1, y0 + fold), (x1, y1), (x0, y1), (x0, y0))
    p.line((x1 - fold, y0), (x1 - fold, y0 + fold), (x1, y0 + fold), caps=False)


def _magnifier(p: _Pen, cx=0.44, cy=0.44, r=0.26) -> None:
    p.circle(cx, cy, r)
    k = 0.7071
    p.line((cx + r * k, cy + r * k), (0.88, 0.88))


def _padlock(p: _Pen, closed: bool = True) -> None:
    p.rect(0.22, 0.46, 0.78, 0.90, r=0.08)
    if closed:
        p.arc(0.50, 0.46, 0.18, 180, 360)
        p.line((0.32, 0.46), (0.32, 0.40), caps=False)
        p.line((0.68, 0.46), (0.68, 0.40), caps=False)
    else:
        # Aabnet: buen er drejet vaek fra laasen (dekrypteret)
        p.arc(0.72, 0.44, 0.18, 180, 330)
        p.line((0.54, 0.44), (0.54, 0.40), caps=False)
    p.fcircle(0.50, 0.66, 0.06)


def _letter_a(p: _Pen, x0=0.22, x1=0.78, y0=0.18, y1=0.72) -> None:
    """'A' tegnet med tre streger (ingen font -- se modulets docstring)."""
    cx = (x0 + x1) / 2.0
    p.line((x0, y1), (cx, y0), (x1, y1))
    bar = y0 + (y1 - y0) * 0.68
    t = 0.68 * 0.5
    p.line((x0 + (cx - x0) * t, bar), (x1 - (x1 - cx) * t, bar), caps=False)


def _text_lines(p: _Pen, y_values, x0=0.18, x1=0.82) -> None:
    for i, y in enumerate(y_values):
        end = x1 if i % 2 == 0 else x1 - 0.18
        p.line((x0, y), (end, y))


# --------------------------------------------------------------------------
# Ikonerne
# --------------------------------------------------------------------------
@icon("add")
def _i_add(p):
    _page(p, 0.14, 0.10, 0.60, 0.78, fold=0.16)
    p.line((0.55, 0.74), (0.91, 0.74), caps=False)
    p.line((0.73, 0.56), (0.73, 0.92), caps=False)


@icon("update")
def _i_update(p):
    p.arc(0.50, 0.50, 0.32, 40, 320)
    p.arrow_head((0.74, 0.28), -20, 0.24)


def _rotate(p, right: bool) -> None:
    """Bue over en side. Pilespidsen sidder i buens faktiske endepunkt."""
    cx, cy, r = 0.50, 0.42, 0.30
    if right:
        p.arc(cx, cy, r, 180, 8)
        p.arrow_head((cx + r, cy), 90, 0.22)
    else:
        p.arc(cx, cy, r, 172, 0)
        p.arrow_head((cx - r, cy), 90, 0.22)
    p.rect(0.30, 0.56, 0.70, 0.92, r=0.04)


@icon("rotate_left")
def _i_rotate_left(p):
    _rotate(p, right=False)


@icon("rotate_right")
def _i_rotate_right(p):
    _rotate(p, right=True)


@icon("save")
def _i_save(p):
    # Diskette: yderkant, laebe foroven, etiket forneden
    p.line((0.14, 0.14), (0.68, 0.14), (0.86, 0.32), (0.86, 0.86), (0.14, 0.86),
           (0.14, 0.14), caps=False)
    p.rect(0.32, 0.14, 0.66, 0.38)
    p.rect(0.28, 0.56, 0.72, 0.86)


@icon("decrypt")
def _i_decrypt(p):
    _padlock(p, closed=False)


@icon("check")
def _i_check(p):
    p.rect(0.18, 0.16, 0.82, 0.90, r=0.08)
    p.line((0.38, 0.16), (0.38, 0.10), (0.62, 0.10), (0.62, 0.16), caps=False)
    p.line((0.34, 0.56), (0.46, 0.70), (0.68, 0.38))


@icon("guess")
def _i_guess(p):
    # Noegle med "gnist": brute-force gaetning
    p.circle(0.32, 0.62, 0.20)
    p.line((0.46, 0.48), (0.86, 0.14))
    p.line((0.72, 0.28), (0.82, 0.38), caps=False)
    p.line((0.60, 0.40), (0.70, 0.50), caps=False)
    p.line((0.14, 0.20), (0.14, 0.34), caps=False)
    p.line((0.07, 0.27), (0.21, 0.27), caps=False)


@icon("swap")
def _i_swap(p):
    p.line((0.34, 0.86), (0.34, 0.26), caps=False)
    p.arrow_head((0.34, 0.10), -90, 0.20)
    p.line((0.66, 0.14), (0.66, 0.74), caps=False)
    p.arrow_head((0.66, 0.90), 90, 0.20)


@icon("up")
def _i_up(p):
    p.line((0.50, 0.90), (0.50, 0.32), caps=False)
    p.arrow_head((0.50, 0.12), -90, 0.24)


@icon("down")
def _i_down(p):
    p.line((0.50, 0.10), (0.50, 0.68), caps=False)
    p.arrow_head((0.50, 0.88), 90, 0.24)


@icon("top")
def _i_top(p):
    p.line((0.16, 0.14), (0.84, 0.14), caps=False)
    p.line((0.50, 0.92), (0.50, 0.48), caps=False)
    p.arrow_head((0.50, 0.28), -90, 0.24)


@icon("bottom")
def _i_bottom(p):
    p.line((0.16, 0.86), (0.84, 0.86), caps=False)
    p.line((0.50, 0.08), (0.50, 0.52), caps=False)
    p.arrow_head((0.50, 0.72), 90, 0.24)


@icon("delete")
def _i_delete(p):
    p.line((0.14, 0.24), (0.86, 0.24), caps=False)
    p.line((0.38, 0.24), (0.38, 0.14), (0.62, 0.14), (0.62, 0.24), caps=False)
    p.line((0.24, 0.24), (0.30, 0.90), (0.70, 0.90), (0.76, 0.24), caps=False)
    p.line((0.42, 0.40), (0.44, 0.76), caps=False)
    p.line((0.58, 0.40), (0.56, 0.76), caps=False)


def _history_arrow(p, back: bool) -> None:
    """Halvbue med lige hale. Halen adskiller den fra rotations-ikonerne."""
    cx, cy, r = 0.50, 0.60, 0.30
    p.arc(cx, cy, r, 180, 0)
    if back:
        p.line((cx + r, cy), (cx + r, 0.90), caps=False)
        p.arrow_head((cx - r, cy), 90, 0.22)
    else:
        p.line((cx - r, cy), (cx - r, 0.90), caps=False)
        p.arrow_head((cx + r, cy), 90, 0.22)


@icon("undo_preview")
def _i_undo(p):
    _history_arrow(p, back=True)


@icon("redo_preview")
def _i_redo(p):
    _history_arrow(p, back=False)


@icon("ok")
def _i_ok(p):
    p.line((0.16, 0.52), (0.40, 0.76), (0.84, 0.24))


@icon("cancel_preview")
def _i_cancel(p):
    p.line((0.20, 0.20), (0.80, 0.80))
    p.line((0.80, 0.20), (0.20, 0.80))


@icon("zoom_in")
def _i_zoom_in(p):
    _magnifier(p)
    p.line((0.30, 0.44), (0.58, 0.44), caps=False)
    p.line((0.44, 0.30), (0.44, 0.58), caps=False)


@icon("zoom_out")
def _i_zoom_out(p):
    _magnifier(p)
    p.line((0.30, 0.44), (0.58, 0.44), caps=False)


@icon("crop")
def _i_crop(p):
    p.line((0.26, 0.06), (0.26, 0.74), (0.94, 0.74))
    p.line((0.06, 0.26), (0.74, 0.26), (0.74, 0.94))


@icon("remove_crop_preview")
def _i_remove_crop(p):
    p.line((0.22, 0.06), (0.22, 0.70), (0.86, 0.70))
    p.line((0.06, 0.22), (0.70, 0.22), (0.70, 0.86))
    p.line((0.62, 0.62), (0.94, 0.94))
    p.line((0.94, 0.62), (0.62, 0.94))


@icon("settings")
def _i_settings(p):
    cx = cy = 0.50
    p.circle(cx, cy, 0.27)
    p.fcircle(cx, cy, 0.09)
    for k in range(8):
        a = math.radians(k * 45.0)
        dx, dy = math.cos(a), math.sin(a)
        p.line((cx + dx * 0.25, cy + dy * 0.25),
               (cx + dx * 0.44, cy + dy * 0.44), caps=False, w=0.14)


@icon("lock")
def _i_lock(p):
    _padlock(p, closed=True)


@icon("unlock")
def _i_unlock(p):
    """Aabnet haengelaas: filen VAR krypteret og er nu laast op."""
    _padlock(p, closed=False)


@icon("sort")
def _i_sort(p):
    """Sorteringsliste: tre linjer der bliver kortere, med en retningspil."""
    for i, y in enumerate((0.22, 0.46, 0.70)):
        p.line((0.12, y), (0.12 + 0.44 - i * 0.13, y), caps=False)
    p.line((0.80, 0.16), (0.80, 0.76), caps=False)
    p.arrow_head((0.80, 0.90), 90, 0.24)


@icon("tool_highlight")
def _i_tool_highlight(p):
    # Markeringspen: nib + bred streg under
    p.line((0.22, 0.56), (0.60, 0.14), (0.80, 0.34), (0.42, 0.74), (0.22, 0.74),
           (0.22, 0.56), caps=False)
    p.line((0.14, 0.90), (0.86, 0.90), caps=False, w=0.16)


@icon("tool_underline")
def _i_tool_underline(p):
    _letter_a(p, 0.24, 0.76, 0.14, 0.66)
    p.line((0.16, 0.86), (0.84, 0.86), caps=False)


@icon("tool_strike")
def _i_tool_strike(p):
    _letter_a(p, 0.24, 0.76, 0.16, 0.84)
    p.line((0.12, 0.54), (0.88, 0.54), caps=False)


@icon("tool_ink")
def _i_tool_ink(p):
    p.line((0.12, 0.70), (0.30, 0.32), (0.50, 0.68), (0.70, 0.30), (0.88, 0.62))


@icon("tool_line")
def _i_tool_line(p):
    p.line((0.14, 0.86), (0.86, 0.14))


@icon("tool_rect")
def _i_tool_rect(p):
    p.rect(0.14, 0.22, 0.86, 0.78, r=0.04)


@icon("tool_circle")
def _i_tool_circle(p):
    p.circle(0.50, 0.50, 0.36)


@icon("tool_note")
def _i_tool_note(p):
    p.line((0.12, 0.16), (0.88, 0.16), (0.88, 0.66), (0.44, 0.66), (0.26, 0.88),
           (0.26, 0.66), (0.12, 0.66), (0.12, 0.16), caps=False)
    p.line((0.28, 0.34), (0.72, 0.34), caps=False)
    p.line((0.28, 0.50), (0.60, 0.50), caps=False)


@icon("tool_freetext")
def _i_tool_freetext(p):
    p.line((0.16, 0.18), (0.84, 0.18), caps=False)
    p.line((0.50, 0.18), (0.50, 0.82), caps=False)
    p.line((0.34, 0.86), (0.66, 0.86), caps=False)


@icon("tool_hand")
def _i_tool_hand(p):
    # 4-vejs flytte-markoer (panorering)
    c = 0.50
    p.line((c, 0.14), (c, 0.86), caps=False)
    p.line((0.14, c), (0.86, c), caps=False)
    for ang, tip in ((-90, (c, 0.10)), (90, (c, 0.90)),
                     (180, (0.10, c)), (0, (0.90, c))):
        p.arrow_head(tip, ang, 0.18)


@icon("tool_select")
def _i_tool_select(p):
    # Klassisk peger-pil
    p.poly([(0.20, 0.14), (0.20, 0.74), (0.36, 0.58),
            (0.48, 0.84), (0.58, 0.80), (0.46, 0.54), (0.68, 0.54)])


@icon("ocr_run")
def _i_ocr_run(p):
    # Tekstgenkendelse: tekstlinjer med et forstoerrelsesglas over
    p.line((0.12, 0.16), (0.80, 0.16), caps=False)
    p.line((0.12, 0.36), (0.32, 0.36), caps=False)
    _magnifier(p, 0.58, 0.62, 0.22)


@icon("tool_redact")
def _i_tool_redact(p):
    # Boks-maskering: en udfyldt blok inde i en ramme
    p.rect(0.12, 0.20, 0.88, 0.80, r=0.04)
    p.frect(0.24, 0.34, 0.76, 0.66, r=0.02)


@icon("tool_redact_text")
def _i_tool_redact_text(p):
    # Ord-maskering: tekstlinjer hvor én er daekket af en bjaelke
    p.line((0.16, 0.28), (0.84, 0.28), caps=False)
    p.line((0.16, 0.72), (0.66, 0.72), caps=False)
    p.frect(0.14, 0.44, 0.72, 0.58, r=0.02)


@icon("search_redact")
def _i_search_redact(p):
    _magnifier(p, 0.42, 0.42, 0.24)
    p.frect(0.28, 0.38, 0.56, 0.46)


@icon("anonymize")
def _i_anonymize(p):
    # Hoved og skuldre med en maskeringsbjaelke over oejnene.
    p.circle(0.50, 0.32, 0.19)
    p.arc(0.50, 0.98, 0.34, 180, 360)
    p.frect(0.28, 0.26, 0.72, 0.37, r=0.02)


@icon("insert_page")
def _i_insert_page(p):
    # Ny side: dokument med overskrifts-bjaelke + tekstlinjer og et plus-badge
    _page(p, 0.08, 0.08, 0.62, 0.80, fold=0.14)
    p.frect(0.16, 0.22, 0.48, 0.31, r=0.01)
    p.line((0.16, 0.44), (0.54, 0.44), caps=False)
    p.line((0.16, 0.56), (0.54, 0.56), caps=False)
    p.circle(0.72, 0.72, 0.20)
    p.line((0.72, 0.61), (0.72, 0.83), caps=False)
    p.line((0.61, 0.72), (0.83, 0.72), caps=False)


@icon("key")
def _i_key(p):
    # Ren noegle: bow (ring) + skaft + to takker
    p.circle(0.30, 0.34, 0.18)
    p.line((0.42, 0.46), (0.84, 0.88))
    p.line((0.72, 0.76), (0.84, 0.64), caps=False)
    p.line((0.60, 0.64), (0.70, 0.54), caps=False)


@icon("panel_left")
def _i_panel_left(p):
    """Ramme med en udfyldt kolonne til venstre -- vis/skjul sidepanel."""
    p.rect(0.12, 0.18, 0.88, 0.82, r=0.06)
    p.frect(0.16, 0.22, 0.40, 0.78, r=0.04)


@icon("select_pages")
def _i_select_pages(p):
    """To sider med en hake -- vaelg flere sider."""
    p.rect(0.10, 0.10, 0.56, 0.66, r=0.05)
    p.rect(0.26, 0.28, 0.72, 0.84, r=0.05)
    p.line((0.52, 0.60), (0.62, 0.71), (0.86, 0.40))


@icon("clipboard")
def _i_clipboard(p):
    """Klemmebraet med tekstlinjer -- kopier til udklipsholder."""
    p.rect(0.18, 0.16, 0.82, 0.90, r=0.08)
    p.frect(0.36, 0.08, 0.64, 0.22, r=0.04)
    p.line((0.30, 0.44), (0.70, 0.44), caps=False)
    p.line((0.30, 0.58), (0.70, 0.58), caps=False)
    p.line((0.30, 0.72), (0.56, 0.72), caps=False)


@icon("history")
def _i_history(p):
    """Ur med tilbagepil -- fortryd-historik."""
    p.arc(0.52, 0.52, 0.32, 40, 330)
    p.line((0.52, 0.34), (0.52, 0.54), (0.68, 0.62))
    p.line((0.20, 0.30), (0.20, 0.50), caps=False)
    p.arrow_head((0.20, 0.24), -90, 0.17)


@icon("_missing")
def _i_missing(p):
    p.rect(0.12, 0.12, 0.88, 0.88, r=0.12)
    p.arc(0.50, 0.38, 0.15, 180, 25)
    p.line((0.636, 0.444), (0.50, 0.58), (0.50, 0.66))
    p.fcircle(0.50, 0.78, 0.055)


# --------------------------------------------------------------------------
# Rastering
# --------------------------------------------------------------------------
def _rgba(color) -> tuple[int, int, int, int]:
    if isinstance(color, (tuple, list)):
        c = tuple(int(v) for v in color)
        return c if len(c) == 4 else (c[0], c[1], c[2], 255)
    s = str(color).lstrip("#")
    if len(s) == 3:
        s = "".join(ch * 2 for ch in s)
    if len(s) == 6:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
    if len(s) == 8:
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), int(s[6:8], 16))
    raise ValueError("Ugyldig farve: %r" % (color,))


def render_pil(name: str, size: int, color) -> Image.Image:
    """Tegn ``name`` i ``size`` px og ``color``. Cachet paa (navn, stoerrelse, farve)."""
    rgba = _rgba(color)
    key = (name, int(size), rgba)
    cached = _PIL_CACHE.get(key)
    if cached is not None:
        return cached

    fn = _REGISTRY.get(name)
    if fn is None:
        if name not in _warned_missing:
            _warned_missing.add(name)
            logger.warning("Ukendt ikon '%s' - bruger placeholder", name)
        fn = _REGISTRY["_missing"]

    big = int(size) * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    width = max(1, int(round(big * STROKE)))
    pen = _Pen(d, big, rgba, width)
    try:
        fn(pen)
    except Exception as e:  # en enkelt daarlig tegnefunktion maa ikke vaelte UI'et
        logger.error("Tegning af ikon '%s' fejlede: %s", name, e)

    img = img.resize((int(size), int(size)), Image.Resampling.LANCZOS)
    _PIL_CACHE[key] = img
    return img


def clear_pil_cache() -> None:
    _PIL_CACHE.clear()


# --------------------------------------------------------------------------
# Qt-lag
# --------------------------------------------------------------------------
def _dpr() -> float:
    """Skaermens device-pixel-ratio, eller 1.0 foer QApplication findes."""
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        return 1.0
    screen = app.primaryScreen()
    return float(screen.devicePixelRatio()) if screen is not None else 1.0


def pil_to_qpixmap(img: Image.Image, dpr: float = 1.0) -> QPixmap:
    """PIL RGBA -> ``QPixmap``.

    ``QImage`` kopierer ikke bufferen, saa den skal bindes til pixmappen med det
    samme (``QPixmap.fromImage`` kopierer) -- ellers peger billedet paa
    frigivet hukommelse. ``devicePixelRatio`` fortaeller Qt at pixmappen er
    tegnet i device-px, saa den vises i den rigtige *logiske* stoerrelse.
    """
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimg = QImage(data, img.width, img.height, img.width * 4,
                  QImage.Format.Format_RGBA8888)
    pm = QPixmap.fromImage(qimg.copy())
    pm.setDevicePixelRatio(dpr)
    return pm


def qicon(name: str, size: int | None = None, color: str | None = None) -> QIcon:
    """Cachet ``QIcon`` for ``name``.

    Ikonet lægges ind i baade ``Normal``- og ``Disabled``-tilstand, saa Qt
    nedtoner det korrekt naar knappen slaas fra. (8.x maatte gentegne ikonet i
    graa manuelt, fordi ttk ikke rørte ``image=``.)
    """
    size = theme.ICON["cmd"] if size is None else int(size)
    color = theme.C["text"] if color is None else color
    dpr = _dpr()
    key = (name, size, color, round(dpr, 3))
    cached = _QICON_CACHE.get(key)
    if cached is not None:
        return cached

    px = max(8, int(round(size * dpr)))
    ic = QIcon()
    ic.addPixmap(pil_to_qpixmap(render_pil(name, px, color), dpr),
                 QIcon.Mode.Normal, QIcon.State.Off)
    ic.addPixmap(pil_to_qpixmap(render_pil(name, px, theme.C["text_disabled"]), dpr),
                 QIcon.Mode.Disabled, QIcon.State.Off)
    _QICON_CACHE[key] = ic
    return ic


def qpixmap(name: str, size: int | None = None, color: str | None = None) -> QPixmap:
    """Enkelt pixmap (til steder der tegner direkte, fx sidegitterets haengelaas)."""
    size = theme.ICON["small"] if size is None else int(size)
    color = theme.C["text"] if color is None else color
    dpr = _dpr()
    return pil_to_qpixmap(render_pil(name, max(8, int(round(size * dpr))), color), dpr)


def swatch_qicon(hexcolor: str, size: int | None = None) -> QIcon:
    """Solidt farvefelt: knappen viser selve den valgte farve.

    En pen med en smal chip under blev laest som et tegnevaerktoej. Et fyldt
    felt siger "farve" uden forklaring. Kanten goer hvid synlig paa lyst.
    """
    size = theme.ICON["cmd"] if size is None else int(size)
    dpr = _dpr()
    key = ("_swatch", size, hexcolor, theme.C["border_strong"], round(dpr, 3))
    cached = _QICON_CACHE.get(key)
    if cached is not None:
        return cached

    s = max(8, int(round(size * dpr)))
    big = s * SS
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((big * 0.08, big * 0.08, big * 0.92, big * 0.92),
                        radius=big * 0.18, fill=_rgba(hexcolor),
                        outline=_rgba(theme.C["border_strong"]),
                        width=max(1, int(round(big * 0.05))))
    img = img.resize((s, s), Image.Resampling.LANCZOS)
    ic = QIcon(pil_to_qpixmap(img, dpr))
    _QICON_CACHE[key] = ic
    return ic


def clear_qicon_cache() -> None:
    """Ryd QIcon-cachen (fx naar skaermens DPI skifter)."""
    _QICON_CACHE.clear()
