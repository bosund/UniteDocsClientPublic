"""Canvas <-> unrotated-PDF-point mapping for the page-view preview (Fase 6).

Annotation geometry is stored in **unrotated source-page points** (see edit_model
/ plan §1.3). The preview shows the page at its *display* orientation: the page's
own ``/Rotate`` (respected by ``get_pixmap``) plus the user's rotation delta
(applied as a PIL rotate in ``pdf_renderer``). Both are clockwise, so the total
rotation from unrotated points to the shown image is ``(page_rotate + user_delta)``.

The 90°-multiple mapping below was calibrated **empirically** against actual
rendering (all 16 page-rotate × user-delta combinations map a known unrotated
point onto the rendered blob to sub-pixel accuracy) rather than trusted from
memory — the same discipline the plan used for ``get_text``/``add_*_annot``.

Pure/Tk-free so it is unit-testable headless.
"""

from __future__ import annotations


class ViewTransform:
    def __init__(self, unrot_w: float, unrot_h: float, total_deg: int,
                 scale: float, off_x: float, off_y: float, crop=None):
        self.uw = float(unrot_w)
        self.uh = float(unrot_h)
        self.deg = int(total_deg) % 360
        self.s = float(scale)
        self.ox = float(off_x)
        self.oy = float(off_y)
        # Beskaering (uroterede kildepunkter). Er den sat, viser lærredet KUN det
        # rektangel, saa afbildningen skal trække cropens roterede oeverste
        # venstre hjoerne fra. crop=None reducerer alt herunder til nøjagtig den
        # gamle aritmetik, saa de kalibrerede rundtur-tests er uændrede.
        self.crop = tuple(float(v) for v in crop) if crop else None
        self.rx0, self.ry0 = 0.0, 0.0
        if self.crop:
            x0, y0, x1, y1 = self.crop
            pts = [self._rot(x0, y0), self._rot(x1, y0),
                   self._rot(x1, y1), self._rot(x0, y1)]
            self.rx0 = min(px for px, _py in pts)
            self.ry0 = min(py for _px, py in pts)

    def _rot(self, x: float, y: float) -> tuple:
        """Uroteret punkt -> punkt i den roterede sidekasse (uden skalering)."""
        d = self.deg
        if d == 0:
            return x, y
        if d == 90:
            return self.uh - y, x
        if d == 180:
            return self.uw - x, self.uh - y
        return y, self.uw - x

    @classmethod
    def build(cls, page_rotate: int, disp_w: float, disp_h: float,
              user_delta: int, img_w: int, img_h: int,
              canvas_w: int, canvas_h: int) -> "ViewTransform":
        """Construct from the page geometry snapshot and the shown image.

        ``disp_w/disp_h`` is ``page.rect`` (display space, respects ``/Rotate``);
        ``img_w/img_h`` are the final displayed image's pixel dimensions; the
        preview centres the image, hence the offsets.
        """
        page_rotate %= 360
        # Unrotated page dimensions: undo the page's own /Rotate.
        if page_rotate % 180 == 90:
            uw, uh = disp_h, disp_w
        else:
            uw, uh = disp_w, disp_h
        total = (page_rotate + user_delta) % 360
        # The rotated box (points) that the image fills.
        if total % 180 == 90:
            rot_w = uh
        else:
            rot_w = uw
        scale = img_w / rot_w if rot_w else 1.0
        off_x = (canvas_w - img_w) / 2.0
        off_y = (canvas_h - img_h) / 2.0
        return cls(uw, uh, total, scale, off_x, off_y)

    # --- mapping ----------------------------------------------------------
    def pdf_to_canvas(self, x: float, y: float) -> tuple:
        """Unrotated source point -> canvas pixel."""
        rx, ry = self._rot(x, y)
        return (self.ox + (rx - self.rx0) * self.s,
                self.oy + (ry - self.ry0) * self.s)

    def canvas_to_pdf(self, cx: float, cy: float) -> tuple:
        """Canvas pixel -> unrotated source point."""
        rx = (cx - self.ox) / self.s + self.rx0
        ry = (cy - self.oy) / self.s + self.ry0
        d = self.deg
        if d == 0:
            x, y = rx, ry
        elif d == 90:
            x, y = ry, self.uh - rx
        elif d == 180:
            x, y = self.uw - rx, self.uh - ry
        else:                       # 270
            x, y = self.uw - ry, rx
        return x, y

    def clamp_pdf(self, x: float, y: float) -> tuple:
        """Clamp an unrotated point to the visible page rectangle (the crop when
        one is set, otherwise the whole page)."""
        if self.crop:
            x0, y0, x1, y1 = self.crop
            return (min(max(x0, x), x1), min(max(y0, y), y1))
        return (min(max(0.0, x), self.uw), min(max(0.0, y), self.uh))

    def rect_from_canvas(self, cx0, cy0, cx1, cy1) -> tuple:
        """Two canvas corners -> a normalised unrotated (x0,y0,x1,y1) rect."""
        x0, y0 = self.canvas_to_pdf(cx0, cy0)
        x1, y1 = self.canvas_to_pdf(cx1, cy1)
        x0, y0 = self.clamp_pdf(x0, y0)
        x1, y1 = self.clamp_pdf(x1, y1)
        return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
