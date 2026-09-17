"""Velkomstskaermen: det foerste man ser, naar der ikke er en side at vise.

Uden filer stod fladen tidligere som et graat felt med linjen *"Ingen sider at
vise. Tilfoej filer."* Den var korrekt og fuldstaendig uinformativ: en ny bruger
fik hverken at vide hvad programmet kan, eller hvad naeste klik er.

Her tegnes i stedet en kort velkomst -- logoet folder sig fra stort til lille,
og derefter ruller programmets funktioner ind, én linje ad gangen, med
**praecis det ikon der sidder paa knappen** i kommandobaren. Teksterne er
skrevet til den de er til for: sekretaeren, den faglige konsulent, advokat-
sekretaeren. De handler om hvad man slipper for, ikke om hvad programmet
teknisk goer.

**Fladen tegnes -- den bygges ikke af widgets**, som sidegitteret og fremviseren
(``QAbstractScrollArea`` + ``paintEvent``). Det er ikke kun for konsistensens
skyld: koreografien er lettere at styre, naar hvert element blot slaar sin egen
fremdrift op i ét ur, end naar tyve widgets hver skal have sin egen
``QGraphicsOpacityEffect``.

**Logoet tegnes ogsaa** -- som vektor i sit eget 128x128-rum, ligesom ikonerne i
``icons_vector``. Der ligger dermed stadig ikke en eneste billedfil i builden,
og maerket er skarpt i enhver stoerrelse animationen finder paa undervejs.
Geometrien er hentet fra ``logo/logo.svg``; brandfarverne er faste tokens i
``theme`` (et logo skifter ikke tone med systemets tema).

Skaermen vises af :class:`~app.page_view.PageView`, naar modellen er tom, og
forsvinder i samme oejeblik den foerste fil lander. Den tager **ikke** imod
drop selv: ``MainWindow`` lytter paa vinduet, saa et traek fra Stifinder
falder igennem hertil af sig selv.
"""

from __future__ import annotations

from PySide6.QtCore import (QEasingCurve, QEvent, QPointF, QRect, QRectF, Qt,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QCursor, QFontMetrics, QPainter,
                           QPainterPath, QPen, QPixmap, QPolygonF)
from PySide6.QtWidgets import QAbstractScrollArea, QFrame

from . import icons_vector
from . import qt_util
from . import theme
from .localization import LocalizationManager
from .logging_config import get_logger

logger = get_logger(__name__)
_ = LocalizationManager.get_text


# --------------------------------------------------------------------------
# Logoet som vektor
# --------------------------------------------------------------------------
LOGO_BOX = 128.0        # logoets eget koordinatrum (som i logo.svg)

# Opdateringspilenes cirkel. Bemaerk at centrum er (64, 66.5) og ikke (64, 64):
# maerket er punktsymmetrisk om det punkt, ikke om sidens midte. Regnes der med
# (64, 64), bliver den nederste pil et par grader kortere end den oeverste.
_ARC_C = (64.0, 66.5)
_ARC_R = 26.5
_ARC_START = 181.0      # grader, Qt-konvention (0 = hoejre, positiv = opad)
_ARC_SWEEP = -141.0     # med uret paa skaermen
_ARC_W = 8.4

# Pilespidserne. Den nederste er den oeverste spejlet i ``_ARC_C``.
_HEAD = ((36.36, 67.20), (46.64, 71.15), (29.12, 77.34))


def _mirror(pt) -> tuple[float, float]:
    return (2 * _ARC_C[0] - pt[0], 2 * _ARC_C[1] - pt[1])


def _doc_path() -> QPainterPath:
    """Dokumentets krop: afrundet rektangel med afskaaret oeverste hoejre hjoerne."""
    p = QPainterPath()
    p.moveTo(26.0, 10.0)
    p.lineTo(88.5, 10.0)
    p.lineTo(108.5, 30.0)
    p.lineTo(108.5, 110.5)
    p.arcTo(94.5, 103.5, 14.0, 14.0, 0.0, -90.0)      # hoejre nederste hjoerne
    p.lineTo(26.0, 117.5)
    p.arcTo(19.0, 103.5, 14.0, 14.0, -90.0, -90.0)    # venstre nederste
    p.lineTo(19.0, 17.0)
    p.arcTo(19.0, 10.0, 14.0, 14.0, 180.0, -90.0)     # venstre oeverste
    p.closeSubpath()
    return p


def paint_logo(p: QPainter, rect: QRectF) -> None:
    """Tegn Unite Docs-maerket, skaleret ind i ``rect`` (kvadratisk).

    Alt regnes i logoets eget 128x128-rum og skaleres af én transform, saa
    stregbredder og radier foelger stoerrelsen -- ogsaa midt i en animation,
    hvor stoerrelsen er et decimaltal.
    """
    p.save()
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    p.translate(rect.left(), rect.top())
    p.scale(rect.width() / LOGO_BOX, rect.height() / LOGO_BOX)
    p.setPen(Qt.PenStyle.NoPen)

    p.fillPath(_doc_path(), QColor(theme.C["brand_ink"]))

    fold = QPolygonF([QPointF(88.5, 10.0), QPointF(108.5, 30.0), QPointF(88.5, 30.0)])
    p.setBrush(QColor(theme.C["brand_ink_alt"]))
    p.drawPolygon(fold)

    cyan = QColor(theme.C["brand_cyan"])
    pen = QPen(cyan, _ARC_W)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    box = QRectF(_ARC_C[0] - _ARC_R, _ARC_C[1] - _ARC_R, 2 * _ARC_R, 2 * _ARC_R)
    # ``drawArc`` regner i 1/16 grader.
    p.drawArc(box, int(_ARC_START * 16), int(_ARC_SWEEP * 16))
    p.drawArc(box, int((_ARC_START - 180.0) * 16), int(_ARC_SWEEP * 16))

    # Spidserne tegnes med en rund samling, saa de matcher pilenes runde ender.
    head_pen = QPen(cyan, 5.0)
    head_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(head_pen)
    p.setBrush(cyan)
    for pts in (_HEAD, tuple(_mirror(q) for q in _HEAD)):
        p.drawPolygon(QPolygonF([QPointF(*q) for q in pts]))

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(theme.C["brand_blue"]))
    p.drawRoundedRect(QRectF(53.0, 63.9, 22.0, 6.2), 1.0, 1.0)
    p.drawRoundedRect(QRectF(60.9, 55.0, 6.2, 24.0), 1.0, 1.0)
    p.restore()


# --------------------------------------------------------------------------
# Indhold
# --------------------------------------------------------------------------
def _features() -> list[dict]:
    """Programmets funktioner, som brugeren maerker dem.

    ``icons`` er navnene paa **de knapper funktionen sidder paa** -- slaar man
    dem op i ``icons_vector``, er det praecis samme glyf som i kommandobaren og
    vaerktoejslinjen. ``tint`` er tokennavnet paa det foerste ikons farve
    (standard ``accent``). Teksterne bygges her og ikke som en modulkonstant,
    saa de oversaettes igen efter et sprogskift.
    """
    return [
        {"icons": ("crop",),
         "title": _("Beskær"),
         "body": _("Skær skæve scannerkanter og alt for brede margener væk med "
                   "ét træk. Bilaget ser trykklart ud — uden en tur forbi "
                   "kopimaskinen.")},
        {"icons": ("sort", "up", "down"),
         "title": _("Sorter"),
         "body": _("Sæt 40 bilag i rækkefølge efter navn eller dato med ét "
                   "klik. Der ryger kvarteret, hvor du ellers trak filer på "
                   "plads én ad gangen.")},
        {"icons": ("insert_page",),
         "title": _("Indsæt side"),
         "body": _("Læg en forside eller en skilleside ind præcis dér, hvor "
                   "den skal være. Overskrift og tekst skriver du her — ingen "
                   "omvej om Word.")},
        {"icons": ("ocr_run",),
         "title": _("Tekstgenkendelse"),
         "body": _("En scanning er et billede — du kan hverken søge eller markere "
                   "i den. Kør tekstgenkendelse én gang, så opfører siden sig som "
                   "ethvert andet dokument: du kan søge, overstrege og maskere ord.")},
        {"icons": ("tool_redact", "tool_redact_text", "anonymize"),
         # Maskering er destruktiv og er derfor ROED i vaerktoejslinjen. Samme
         # farve her: genkender man ikonet, skal man ogsaa genkende alvoren.
         "tint": "danger",
         "title": _("Masker og anonymiser"),
         "body": _("Den sorte streg fjerner teksten under sig — den lægger sig "
                   "ikke bare ovenpå. Anonymiser gennemgår selv hver eneste "
                   "side for navne, CPR-numre og adresser, så du slipper for "
                   "at nærlæse 200 sider før aktindsigten sendes.")},
        {"icons": ("clipboard",),
         "title": _("Kopiér tekst"),
         "body": _("Skal sagen forbi en AI? Kopiér teksten med navne, CPR-numre "
                   "og adresser byttet ud med Person 1 og Advokat 2 — samme "
                   "person får samme etiket i hele bundtet, så teksten stadig "
                   "giver mening. Nøglen tilbage beholder du selv.")},
        {"icons": ("tool_highlight", "tool_ink", "tool_select"),
         "title": _("Fremhæv, tegn og marker"),
         "body": _("Gul overstregning på det afgørende afsnit, en pil eller en "
                   "note ved siden af. Modtageren ser pointen med det samme — "
                   "også på den udskrevne udgave.")},
        {"icons": ("save", "decrypt"),
         "title": _("Flet og gem"),
         "body": _("Saml hele sagen i én PDF til retten, eller gem hver fil "
                   "for sig. Rækkefølgen bliver den, du ser på skærmen — der "
                   "er ingen overraskelser i den færdige fil.")},
        {"icons": ("key",),
         "title": _("Kodeord"),
         "body": _("Åbn en låst PDF én gang, så husker programmet koden. "
                   "Næste gang — og næste uge — åbner filen af sig selv.")},
    ]


def _links() -> list[dict]:
    return [
        {"url": "https://guru.uniteapps.dk", "label": "guru.uniteapps.dk",
         "desc": _("Vejledninger og svar på det, der ellers koster et opkald "
                   "til IT.")},
        {"url": "https://cvr.uniteapps.dk", "label": "cvr.uniteapps.dk",
         "desc": _("Slå en virksomhed op: CVR-nummer, adresse og ejerkreds på "
                   "få sekunder.")},
        {"url": "https://www.uniteapps.dk", "label": "www.uniteapps.dk",
         "desc": _("Nyheder, opdateringer og de øvrige Unite-programmer.")},
    ]


# --------------------------------------------------------------------------
# Koreografi (millisekunder fra start)
# --------------------------------------------------------------------------
LOGO_HOLD = 240         # logoet staar stort et oejeblik foerst
LOGO_MS = 700           # stort -> lille
TITLE_AT, TITLE_MS = 420, 360
TAG_AT = 560
CTA_AT = 700
SECTION_AT = 780
ROW_AT, ROW_STEP, ROW_MS = 840, 75, 340
FADE_MS = 340
TOTAL_MS = 2400


class WelcomeView(QAbstractScrollArea):
    """Velkomst og funktionsoversigt, tegnet paa ét laerred."""

    add_files_requested = Signal()

    MAXW = 900          # maksimal tekstbredde; laengere linjer bliver ulaeselige
    MARGIN = 22
    LOGO_SMALL = 66
    CARD_PAD = 12
    CARD_GAP = 8
    ICON = 22

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.viewport().setMouseTracking(True)
        self.verticalScrollBar().setSingleStep(28)

        self._ms = float(TOTAL_MS)     # faerdigspillet indtil animationen startes
        self._played = False
        self._hot: list[dict] = []     # klikbare felter i indholdskoordinater
        self._hover: int | None = None
        self._drop = False             # et traek fra Stifinder svaever over os
        self._pix: dict[tuple, QPixmap] = {}
        self._layout: dict = {}

        self._anim = QVariantAnimation(self)
        self._anim.setStartValue(0.0)
        self._anim.setEndValue(float(TOTAL_MS))
        self._anim.setDuration(TOTAL_MS)
        self._anim.setEasingCurve(QEasingCurve.Type.Linear)
        self._anim.valueChanged.connect(self._on_tick)
        # Ingen ``theme.on_scheme_changed`` her: hooket ville overleve widget'en
        # (den rives ned ved sprogskift). ``PageView.refresh_themed_icons``
        # kalder ``refresh_theme`` for os.

    # ---------------------------------------------------------------- api
    def showEvent(self, event):  # noqa: N802 - Qt-API
        # Animationen starter foerst her, ikke naar widget'en bygges: bliver
        # vinduet et halvt sekund om at komme frem, er den ellers halvt forbi
        # naar man endelig ser den.
        super().showEvent(event)
        self.play()

    def play(self) -> None:
        """Spil velkomsten -- én gang pr. kørsel.

        Tømmer man kurven igen midt i en arbejdsdag, skal skaermen vaere der
        med det samme; den samme animation to gange er en forsinkelse, ikke en
        oplevelse.
        """
        if self._played:
            self._ms = float(TOTAL_MS)
            self.viewport().update()
            return
        self._played = True
        self._ms = 0.0
        self._anim.start()

    def refresh_theme(self) -> None:
        """Ikonerne er tegnet i en tokenfarve og skal gentegnes ved temaskift."""
        self._pix.clear()
        self._compute_layout()
        self.viewport().update()

    def set_drop_highlight(self, on: bool) -> None:
        """Lys feltet op, mens et traek fra Stifinder svaever over vinduet."""
        on = bool(on)
        if on != self._drop:
            self._drop = on
            self.viewport().update()

    # ------------------------------------------------------------ hjaelpere
    def _on_tick(self, value) -> None:
        self._ms = float(value)
        self.viewport().update()

    @staticmethod
    def _ease(t: float) -> float:
        """Ud-af-en-tredjegrad: hurtigt i gang, blødt i mål."""
        t = max(0.0, min(1.0, t))
        return 1.0 - (1.0 - t) ** 3

    @staticmethod
    def _ease_inout(t: float) -> float:
        """Blødt i begge ender -- til logoets rejse, som man ser paa.

        Et rent ``ease-out`` river maerket ned i stoerrelse med det samme; her
        saetter det i gang, og lander lige saa blødt."""
        t = max(0.0, min(1.0, t))
        return 4 * t ** 3 if t < 0.5 else 1.0 - (-2 * t + 2) ** 3 / 2

    def _phase(self, start: int, dur: int) -> float:
        if self._ms >= start + dur:
            return 1.0
        if self._ms <= start:
            return 0.0
        return self._ease((self._ms - start) / float(dur))

    def _pixmap(self, name: str, size: int, color: str) -> QPixmap:
        key = (name, size, color)
        pm = self._pix.get(key)
        if pm is None:
            pm = icons_vector.qpixmap(name, size, color)
            self._pix[key] = pm
        return pm

    def _offset(self) -> int:
        return self.verticalScrollBar().value()

    # ------------------------------------------------------------- layout
    def resizeEvent(self, event):  # noqa: N802 - Qt-API
        super().resizeEvent(event)
        self._compute_layout()
        self.viewport().update()

    def _compute_layout(self) -> None:
        """Regn alle rektangler ud én gang, i indholdskoordinater.

        Kortene laegges i to spalter, naar der er plads til at teksten stadig
        kan laeses -- ellers i én. Alle kort i samme raekke faar den hoejeste
        af raekkens hoejder, saa gitteret staar lige; et enligt sidste kort
        faar hele bredden i stedet for at haenge halvt ude i luften.
        """
        vw = max(320, self.viewport().width())
        colw = min(self.MAXW, vw - 2 * self.MARGIN)
        x0 = (vw - colw) // 2
        L: dict = {"x0": x0, "colw": colw}

        fm_disp = QFontMetrics(theme.font("display"))
        fm_lead = QFontMetrics(theme.font("lead"))
        fm_base = QFontMetrics(theme.font("base"))
        fm_head = QFontMetrics(theme.font("heading"))
        fm_small = QFontMetrics(theme.font("small"))
        fm_link = QFontMetrics(theme.font("link"))
        wrap = int(Qt.TextFlag.TextWordWrap)

        y = self.MARGIN
        L["logo"] = QRect(x0 + (colw - self.LOGO_SMALL) // 2, y,
                          self.LOGO_SMALL, self.LOGO_SMALL)
        y += self.LOGO_SMALL + theme.SPACE["sm"]
        L["title"] = QRect(x0, y, colw, fm_disp.height())
        y += fm_disp.height() + theme.SPACE["xs"]
        L["tag"] = QRect(x0, y, colw, fm_lead.height() + theme.SPACE["xs"])
        y += L["tag"].height() + theme.SPACE["lg"]
        L["cta"] = QRect(x0 + colw // 6, y, colw - 2 * (colw // 6),
                         fm_base.height() + 2 * theme.SPACE["sm"])
        y += L["cta"].height() + theme.SPACE["xl"]

        L["section"] = QRect(x0, y, colw, fm_head.height())
        y += fm_head.height() + theme.SPACE["sm"]

        feats = _features()
        cols = 2 if colw >= 620 else 1
        cw = (colw - self.CARD_GAP) // 2 if cols == 2 else colw
        text_x = self.CARD_PAD + self.ICON + theme.SPACE["md"]
        L["text_x"] = text_x

        def card_width(i: int) -> int:
            # Et enligt kort i den sidste raekke fylder hele spalten.
            alone = cols == 2 and i == len(feats) - 1 and i % cols == 0
            return colw if (cols == 1 or alone) else cw

        def card_height(w: int, f: dict) -> int:
            body_w = max(60, w - text_x - self.CARD_PAD)
            body_h = fm_base.boundingRect(QRect(0, 0, body_w, 10000),
                                          wrap, f["body"]).height()
            text_h = fm_head.height() + theme.SPACE["xs"] + body_h
            n = len(f["icons"])
            icon_h = n * self.ICON + (n - 1) * theme.SPACE["xs"]
            return 2 * self.CARD_PAD + max(text_h, icon_h)

        widths = [card_width(i) for i in range(len(feats))]
        heights = [card_height(widths[i], f) for i, f in enumerate(feats)]

        cards = []
        for first in range(0, len(feats), cols):
            group = range(first, min(first + cols, len(feats)))
            h = max(heights[i] for i in group)
            for i in group:
                cards.append({**feats[i],
                              "rect": QRect(x0 + (i - first) * (cw + self.CARD_GAP),
                                            y, widths[i], h),
                              "body_w": max(60, widths[i] - text_x - self.CARD_PAD)})
            y += h + self.CARD_GAP
        L["cards"] = cards
        y += theme.SPACE["xl"] - self.CARD_GAP

        L["links_title"] = QRect(x0, y, colw, fm_head.height())
        y += fm_head.height() + theme.SPACE["sm"]

        links = _links()
        lcols = 3 if colw >= 620 else 1
        lw = (colw - (lcols - 1) * self.CARD_GAP) // lcols
        # Ens hoejde paa alle tre kort: den laengste forklaring saetter maalet.
        longest = max((d["desc"] for d in links), key=len)
        lh = (2 * self.CARD_PAD + fm_link.height() + theme.SPACE["xxs"]
              + fm_small.boundingRect(
                  QRect(0, 0, max(60, lw - 2 * self.CARD_PAD), 10000),
                  wrap, longest).height())
        out = []
        for i, d in enumerate(links):
            col, row = i % lcols, i // lcols
            out.append({**d, "rect": QRect(x0 + col * (lw + self.CARD_GAP),
                                           y + row * (lh + self.CARD_GAP),
                                           lw, lh)})
        L["links"] = out
        y += ((len(links) + lcols - 1) // lcols) * (lh + self.CARD_GAP)
        y += self.MARGIN

        page = max(1, self.viewport().height())
        # Er der plads til overs, saettes hele saettet lodret i midten. Paa en
        # bred skaerm stod velkomsten ellers klemt op i toppen med en halv
        # skaerm tomhed under sig.
        shift = max(0, (page - y) // 2)
        if shift:
            for key in ("logo", "title", "tag", "cta", "section", "links_title"):
                L[key].translate(0, shift)
            for d in L["cards"] + L["links"]:
                d["rect"].translate(0, shift)

        self._layout = L
        self._rebuild_hotspots()

        sb = self.verticalScrollBar()
        sb.setPageStep(page)
        sb.setRange(0, max(0, y - page))

    def _rebuild_hotspots(self) -> None:
        L = self._layout
        self._hot = [{"rect": L["cta"], "kind": "add"}]
        for l in L.get("links", ()):
            self._hot.append({"rect": l["rect"], "kind": "url", "url": l["url"]})

    # -------------------------------------------------------------- maling
    def paintEvent(self, event):  # noqa: N802 - Qt-API
        if not self._layout:
            self._compute_layout()
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor(theme.C["bg"]))
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.translate(0, -self._offset())
        L = self._layout

        self._paint_hero(p, L)
        self._paint_text(p, L["section"], _("Det kan du med Unite Docs"),
                         theme.font("heading"), theme.C["text"],
                         self._phase(SECTION_AT, FADE_MS))
        for i, card in enumerate(L["cards"]):
            self._paint_card(p, card, self._phase(ROW_AT + i * ROW_STEP, ROW_MS))
        last = ROW_AT + (len(L["cards"]) - 1) * ROW_STEP + ROW_MS
        self._paint_text(p, L["links_title"], _("Mere fra Unite Apps"),
                         theme.font("heading"), theme.C["text"],
                         self._phase(last, FADE_MS))
        for i, link in enumerate(L["links"]):
            self._paint_link(p, link, i, self._phase(last + 60 + i * 60, FADE_MS))
        p.end()

    def _paint_hero(self, p: QPainter, L: dict) -> None:
        """Logoet fra stort og midt i fladen til lille over overskriften."""
        small = QRectF(L["logo"])
        t = self._ease_inout((self._ms - LOGO_HOLD) / float(LOGO_MS))
        if t < 1.0:
            big_size = max(float(self.LOGO_SMALL),
                           min(self.viewport().height() * 0.46, L["colw"] * 0.42, 300.0))
            size = big_size + (self.LOGO_SMALL - big_size) * t
            cx = small.center().x()
            cy_big = self.viewport().height() / 2.0
            cy = cy_big + (small.center().y() - cy_big) * t
            box = QRectF(cx - size / 2.0, cy - size / 2.0, size, size)
            p.setOpacity(min(1.0, self._phase(0, 260)))
        else:
            box = small
            p.setOpacity(1.0)
        paint_logo(p, box)
        p.setOpacity(1.0)

        self._paint_text(p, L["title"], _("Unite Docs"), theme.font("display"),
                         theme.C["text"], self._phase(TITLE_AT, TITLE_MS),
                         align=Qt.AlignmentFlag.AlignHCenter)
        self._paint_text(p, L["tag"],
                         _("Saml sagen, ryd op og send den af sted — uden at "
                           "printe en eneste side."),
                         theme.font("lead"), theme.C["text_muted"],
                         self._phase(TAG_AT, FADE_MS),
                         align=Qt.AlignmentFlag.AlignHCenter)
        self._paint_cta(p, L["cta"], self._phase(CTA_AT, FADE_MS))

    def _paint_cta(self, p: QPainter, rect: QRect, t: float) -> None:
        """Naeste klik, sagt højt. Hele feltet er klikbart."""
        if t <= 0.0:
            return
        p.save()
        p.setOpacity(t)
        p.translate(0, (1.0 - t) * 10)
        hot = self._drop or (self._hover is not None
                             and self._hot[self._hover]["kind"] == "add")
        r = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setBrush(QColor(theme.C["accent_subtle"] if hot else theme.C["surface"]))
        pen = QPen(QColor(theme.C["accent"] if hot else theme.C["border_strong"]), 1)
        pen.setStyle(Qt.PenStyle.SolidLine if hot else Qt.PenStyle.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, 8, 8)

        p.setPen(QColor(theme.C["text"]))
        p.setFont(theme.font("base"))
        p.drawText(rect, int(Qt.AlignmentFlag.AlignCenter),
                   _("Træk dine PDF'er herind — eller klik for at finde dem"))
        p.restore()

    def _paint_text(self, p: QPainter, rect: QRect, text: str, font,
                    color: str, t: float,
                    align=Qt.AlignmentFlag.AlignLeft) -> None:
        if t <= 0.0:
            return
        p.save()
        p.setOpacity(t)
        p.translate(0, (1.0 - t) * 10)
        p.setFont(font)
        p.setPen(QColor(color))
        p.drawText(rect, int(align | Qt.AlignmentFlag.AlignVCenter), text)
        p.restore()

    def _paint_card(self, p: QPainter, card: dict, t: float) -> None:
        if t <= 0.0:
            return
        p.save()
        p.setOpacity(t)
        p.translate(0, (1.0 - t) * 14)
        r = card["rect"]
        p.setBrush(QColor(theme.C["surface"]))
        p.setPen(QPen(QColor(theme.C["border"]), 1))
        p.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        x = r.left() + self.CARD_PAD
        y = r.top() + self.CARD_PAD
        tint = card.get("tint", "accent")
        for j, name in enumerate(card["icons"]):
            # Kun det foerste ikon staar i fuld farve: de oevrige er varianter
            # af samme funktion og maa ikke stjaele blikket.
            color = theme.C[tint] if j == 0 else theme.C["text_muted"]
            pm = self._pixmap(name, self.ICON, color)
            p.drawPixmap(QRect(x, y + j * (self.ICON + theme.SPACE["xs"]),
                               self.ICON, self.ICON), pm)

        tx = r.left() + self._layout["text_x"]
        fm_head = QFontMetrics(theme.font("heading"))
        p.setFont(theme.font("heading"))
        p.setPen(QColor(theme.C["text"]))
        p.drawText(QRect(tx, y, card["body_w"], fm_head.height()),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   card["title"])

        p.setFont(theme.font("base"))
        p.setPen(QColor(theme.C["text_muted"]))
        p.drawText(QRect(tx, y + fm_head.height() + theme.SPACE["xs"],
                         card["body_w"],
                         r.bottom() - y - fm_head.height() - self.CARD_PAD),
                   int(Qt.TextFlag.TextWordWrap), card["body"])
        p.restore()

    def _paint_link(self, p: QPainter, link: dict, index: int, t: float) -> None:
        if t <= 0.0:
            return
        hot = (self._hover is not None
               and self._hot[self._hover].get("url") == link["url"])
        p.save()
        p.setOpacity(t)
        p.translate(0, (1.0 - t) * 14)
        r = link["rect"]
        p.setBrush(QColor(theme.C["accent_subtle"] if hot else theme.C["surface"]))
        p.setPen(QPen(QColor(theme.C["accent"] if hot else theme.C["border"]), 1))
        p.drawRoundedRect(QRectF(r).adjusted(0.5, 0.5, -0.5, -0.5), 8, 8)

        fm_link = QFontMetrics(theme.font("link"))
        x = r.left() + self.CARD_PAD
        w = r.width() - 2 * self.CARD_PAD
        p.setFont(theme.font("link"))
        p.setPen(QColor(theme.C["link"]))
        p.drawText(QRect(x, r.top() + self.CARD_PAD, w, fm_link.height()),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   link["label"])
        p.setFont(theme.font("small"))
        p.setPen(QColor(theme.C["text_muted"]))
        p.drawText(QRect(x, r.top() + self.CARD_PAD + fm_link.height()
                         + theme.SPACE["xxs"], w,
                         r.bottom() - r.top() - self.CARD_PAD - fm_link.height()),
                   int(Qt.TextFlag.TextWordWrap), link["desc"])
        p.restore()

    # ---------------------------------------------------------------- mus
    def _hit(self, pos) -> int | None:
        y = pos.y() + self._offset()
        for i, h in enumerate(self._hot):
            if h["rect"].contains(pos.x(), y):
                return i
        return None

    def mouseMoveEvent(self, event):  # noqa: N802 - Qt-API
        hit = self._hit(event.position().toPoint())
        if hit != self._hover:
            self._hover = hit
            self.viewport().setCursor(QCursor(
                Qt.CursorShape.PointingHandCursor if hit is not None
                else Qt.CursorShape.ArrowCursor))
            self.viewport().update()

    def viewportEvent(self, event):  # noqa: N802 - Qt-API
        # ``QAbstractScrollArea`` videresender mus og hjul til vores egne
        # handlere, men IKKE ``Leave``. Uden dette bliver et kort haengende i
        # hover-farven, naar musen forlader fladen i ét ryk.
        if event.type() == QEvent.Type.Leave and self._hover is not None:
            self._hover = None
            self.viewport().update()
        return super().viewportEvent(event)

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt-API
        if event.button() != Qt.MouseButton.LeftButton:
            return
        hit = self._hit(event.position().toPoint())
        if hit is None:
            return
        h = self._hot[hit]
        if h["kind"] == "add":
            self.add_files_requested.emit()
        else:
            qt_util.open_url(h["url"])
