"""Designtokens for Unite Docs' brugerflade (PySide6).

Dette modul er **eneste sandhed** for farve, spacing og font. Ingen anden fil i
``client/app`` maa indeholde et haardkodet ``#RRGGBB``-litteral (haandhaevet af
``tests/test_theme_tokens.py``).

Chromet leveres af Qt's **native** ``windows11``-stil: knapper, felter, ruller og
menuer tegnes af Windows selv, saa de foelger systemets accentfarve og
animationer. Vi styler dem *ikke* om. ``C`` daekker derfor kun de flader Qt ikke
har en mening om -- fremviserens graa baggrund, sidefliser, drop-indikatorer,
maskeringsfarver -- plus de faa semantiske tekstfarver (link, danger, muted).

**Lys og moerk tilstand** styres af Qt: ``set_color_scheme`` saetter
``QStyleHints.colorScheme``, hvorefter den native palet skifter og
``sync_tokens_from_palette`` laeser den nye. Derfor findes der ingen anden
"moerk palet" i koden -- kun de faa *semantiske* farver har et par, fordi de
ikke kan udledes (en roed fejlfarve skal vaere moerk paa lyst og lys paa moerkt).

Vaerdierne nedenfor er **startvaerdier**. ``apply_theme`` overskriver de
neutrale af dem med det Qt's native palet faktisk rapporterer
(``sync_tokens_from_palette``), saa vores selvtegnede flader matcher chromet
paa den maskine appen koerer paa -- inklusive brugerens egen accentfarve.

8.x maatte spejle sv-ttk's spritesheet i haanden, fordi et pixmap-tema ikke
kunne spoerges om noget. Her er der en kilde, saa vi laeser den i stedet for at
gaette. Og vi saetter **ikke** paletten selv: gjorde vi det, mistede knapperne
deres i forvejen diskrete Windows 11-kant.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication, QFrame, QStyleFactory

from .logging_config import get_logger

logger = get_logger(__name__)


# --------------------------------------------------------------------------
# Farver
# --------------------------------------------------------------------------
# Startvaerdier. De neutrale (bg/surface/border/text/accent/...) erstattes af
# ``sync_tokens_from_palette`` ved opstart; de staar her, saa modulet kan
# importeres og testes uden en ``QApplication``. De semantiske
# (danger/success/warning, page_gutter, paper, redact_bar) er faste.
C = {
    "bg":            "#fafafa",   # vinduets flade
    "bg_chrome":     "#fafafa",   # kommandobar/statuslinje
    "bg_sunken":     "#f0f0f0",   # nedsaenket omraade (miniaturegitter)
    "surface":       "#fdfdfd",   # haevet flade: kort, fliser
    "border":        "#e7e7e7",   # haarstreg / kortkant
    "border_strong": "#d6d6d6",   # tydelig kant
    "text":          "#1c1c1c",
    "text_muted":    "#626262",
    "text_disabled": "#a0a0a0",
    "accent":        "#005fb8",
    "accent_subtle": "#eaf2fb",   # accent som lys fyldfarve (markeret flise)
    "selection":     "#2f60d8",   # tekstmarkering
    "selection_fg":  "#ffffff",
    "hover":         "#f9f9f9",
    "danger":        "#c42b1c",
    "success":       "#0f7b0f",
    "warning":       "#9d5d00",
    "link":          "#005fb8",
    "page_gutter":   "#54595f",   # moerk baggrund bag PDF-sider
    "page_gutter_fg":"#d6d6d6",   # lys tekst paa den moerke fremviser-baggrund
    "page_shadow":   "#2b2b2b",
    "paper":         "#ffffff",   # hvidt PDF-papir
    "tile_bg":       "#f0f0f0",   # sideflise i hvile
    "drop_target":   "#005fb8",   # ramme om drop-maalet
    "drop_line":     "#005fb8",   # indsaetnings-indikator
    "redact_bar":    "#000000",   # maskering er altid sort (semantisk, ikke tema)
    # Logoets brandfarver. FASTE -- som ``redact_bar``. Et logo skifter ikke
    # tone med systemets tema, og velkomstskaermen tegner maerket direkte fra
    # disse tokens (kilde: ``logo/logo.svg``).
    "brand_ink":     "#2f3d6e",   # dokumentets krop
    "brand_ink_alt": "#1e2454",   # aeselore-folden
    "brand_cyan":    "#55d1ed",   # opdateringspilene
    "brand_blue":    "#02ade5",   # plusset
}


# Semantiske farver kan ikke udledes af paletten: en fejlfarve skal vaere moerk
# paa lys baggrund og lys paa moerk. (lys, moerk)
_SEMANTIC = {
    "danger":         ("#c42b1c", "#ff99a4"),
    "success":        ("#0f7b0f", "#6ccb5f"),
    "warning":        ("#9d5d00", "#fce100"),
    "page_gutter":    ("#54595f", "#303030"),
    "page_gutter_fg": ("#d6d6d6", "#9d9d9d"),
    "page_shadow":    ("#2b2b2b", "#000000"),
}


def is_dark() -> bool:
    """Er den aktive palet moerk? Afgoeres paa vinduesfarvens lyshed, ikke paa
    ``colorScheme()``: sidstnaevnte er ``Unknown`` naar vi foelger systemet."""
    return QColor(C["bg"]).lightness() < 128


def qc(key: str) -> QColor:
    """Token som ``QColor``. Ukendt noegle giver tekstfarven frem for et brag."""
    return QColor(C.get(key, C["text"]))


# --------------------------------------------------------------------------
# Spacing (logiske px -- Qt skalerer selv efter skaermens DPI)
# --------------------------------------------------------------------------
SPACE = {"xxs": 2, "xs": 4, "sm": 6, "md": 8, "lg": 12, "xl": 16, "xxl": 24}

# --------------------------------------------------------------------------
# Fonte. Segoe UI Variable Text er Windows 11's UI-font; Segoe UI er fallback
# paa aeldre versioner. Stoerrelserne er punkter, som i 8.x.
# --------------------------------------------------------------------------
_FAMILY = "Segoe UI Variable Text"
_FAMILY_FALLBACK = "Segoe UI"

# (punktstoerrelse, fed, kursiv, understreget)
FONTS = {
    "base":    (9,  False, False, False),
    "strong":  (9,  True,  False, False),
    "small":   (8,  False, False, False),
    "caption": (8,  False, False, False),
    "title":   (12, True,  False, False),
    "link":    (9,  False, False, True),
    "italic":  (9,  False, True,  False),
    "heading": (10, True,  False, False),
    # Velkomstskaermens hero. Stoerre end noget andet i fladen -- den er det
    # foerste man ser, og den er der kun, naar der ikke er en side at vise.
    "display": (21, True,  False, False),
    "lead":    (11, False, False, False),
}


def font(name: str = "base") -> QFont:
    """Byg en ``QFont`` fra et token. Familien har fallback-kaeden indbygget."""
    size, bold, italic, underline = FONTS.get(name, FONTS["base"])
    f = QFont(_FAMILY, size)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    f.setFamilies([_FAMILY, _FAMILY_FALLBACK, "Segoe UI", "sans-serif"])
    f.setBold(bold)
    f.setItalic(italic)
    f.setUnderline(underline)
    return f


# Ikonstoerrelser (logiske px). cmdlg = store kommandobar-ikoner (ikon over
# tekst), cmd = kompakt kommandobar, tool = vaerktoejslinje.
ICON = {"cmdlg": 24, "cmd": 20, "tool": 18, "small": 16, "tiny": 14}

TOOLTIP_DELAY_MS = 450


# --------------------------------------------------------------------------
# Tema
# --------------------------------------------------------------------------
def sync_tokens_from_palette(palette: QPalette) -> None:
    """Traek de neutrale tokens ud af Qt's **native** palet.

    Det er den ene ting der holder vores selvtegnede flader (sidegitteret,
    fremviseren, statuslinjen) i sync med det chrome Windows tegner. 8.x maatte
    spejle sv-ttk's spritesheet i haanden, fordi et pixmap-tema ikke kunne
    spoerges; her *er* der en kilde at spoerge, saa vi bruger den.

    Bonus: ``accent``/``selection`` bliver brugerens egen Windows-accentfarve i
    stedet for en fast blaa.

    Vi saetter til gengaeld **ikke** paletten selv. Overskrev vi fx ``Button``
    med et naesten-hvidt token, forsvandt knappernes i forvejen diskrete
    Windows 11-kant helt -- den native palet er kalibreret til stilens egen
    tegning og skal have lov at staa.
    """
    act, dis = QPalette.ColorGroup.Active, QPalette.ColorGroup.Disabled
    R = QPalette.ColorRole

    def col(role, group=act) -> QColor:
        return palette.color(group, role)

    window = col(R.Window)
    dark = window.lightness() < 128

    C["bg"] = window.name()
    C["bg_chrome"] = window.name()
    C["surface"] = col(R.Base).name()
    C["text"] = col(R.WindowText).name()
    C["text_disabled"] = col(R.Text, dis).name()
    C["selection"] = col(R.Highlight).name()
    C["selection_fg"] = col(R.HighlightedText).name()
    C["accent"] = col(R.Highlight).name()
    C["link"] = col(R.Highlight).name()

    # Afledte flader. Windows 11 har ingen palet-rolle for "nedsaenket" eller
    # "haarstreg", saa de udledes af vinduesfarven -- og retningen VENDER i
    # moerk tilstand: dér ligger en kant og en haevet flade *lysere* end
    # baggrunden, ikke moerkere.
    if dark:
        C["bg_sunken"] = window.darker(135).name()
        C["hover"] = window.lighter(140).name()
        C["border"] = window.lighter(190).name()
        C["border_strong"] = window.lighter(260).name()
        C["tile_bg"] = window.lighter(120).name()
        C["paper"] = "#ffffff"          # PDF-papir er hvidt uanset tema
    else:
        C["bg_sunken"] = window.darker(104).name()
        C["hover"] = window.lighter(103).name()
        C["border"] = window.darker(108).name()
        C["border_strong"] = window.darker(118).name()
        C["tile_bg"] = window.darker(103).name()
        C["paper"] = "#ffffff"

    C["text_muted"] = _blend(col(R.WindowText), window, 0.40).name()
    C["accent_subtle"] = _blend(col(R.Highlight), window, 0.80 if dark else 0.88).name()
    C["drop_target"] = C["accent"]
    C["drop_line"] = C["accent"]

    for key, (light_v, dark_v) in _SEMANTIC.items():
        C[key] = dark_v if dark else light_v
    # redact_bar er ALTID sort: en maskering er sort i den gemte PDF, og
    # forhaandsvisningen skal vise det rigtige -- ikke temaets tone.


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    """Lineaer blanding: ``t=0`` giver ``a``, ``t=1`` giver ``b``."""
    t = max(0.0, min(1.0, t))
    return QColor(int(a.red() + (b.red() - a.red()) * t),
                  int(a.green() + (b.green() - a.green()) * t),
                  int(a.blue() + (b.blue() - a.blue()) * t))


# Kun det Qt's native stil ikke daekker. Holdes bevidst kort: hver linje her er
# en flade Windows ikke har en mening om, ikke en omstyling af chromet.
_QSS = """
QToolButton#CmdButton {
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 4px 8px 5px 8px;
}
QToolButton#CmdButton:hover   { background: %(hover)s; border-color: %(border)s; }
QToolButton#CmdButton:pressed { background: %(bg_sunken)s; }
QToolButton#CmdButton:checked { background: %(accent_subtle)s; border-color: %(accent)s; }

QToolButton#CmdPrimary {
    border: 1px solid %(accent)s;
    border-radius: 5px;
    padding: 4px 8px 5px 8px;
    background: %(accent)s;
    color: %(selection_fg)s;
}
QToolButton#CmdPrimary:hover   { background: %(accent_hover)s; }
QToolButton#CmdPrimary:pressed { background: %(accent_press)s; }
QToolButton#CmdPrimary:disabled {
    background: %(bg_sunken)s; border-color: %(border)s; color: %(text_disabled)s;
}

QToolButton#ToolIcon {
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 2px;
}
QToolButton#ToolIcon:hover   { background: %(hover)s; border-color: %(border)s; }
QToolButton#ToolIcon:checked { background: %(accent_subtle)s; border-color: %(accent)s; }

QFrame#Hairline { background: %(border)s; border: none; }

QListWidget#FileList {
    background: %(bg)s;
    border: none;
    border-right: 1px solid %(border)s;
    outline: none;
}
QListWidget#FileList::item {
    /* Ingen padding her: hoejden kommer fra delegatens sizeHint, og QSS-padding
       laegges oveni uden at sizeHint ved det -- teksten blev klippet. */
    padding: 0px;
    border-radius: 4px;
    margin: 1px 4px;
    color: %(text)s;
}
QListWidget#FileList::item:hover    { background: %(hover)s; }
QListWidget#FileList::item:selected { background: %(accent_subtle)s; color: %(text)s; }
QWidget#CommandBar, QWidget#StatusBar { background: %(bg_chrome)s; }
QLabel#Muted   { color: %(text_muted)s; }
QLabel#Danger  { color: %(danger)s; }
QLabel#Success { color: %(success)s; }
QLabel#Warning { color: %(warning)s; }
QLabel#Link    { color: %(link)s; text-decoration: underline; }
QLabel#SectionTitle { color: %(text)s; }
"""


def _accent_variants() -> dict:
    """Hover/pressed-toner af accenten, udledt frem for hardkodet."""
    base = qc("accent")
    return {
        "accent_hover": base.lighter(112).name(),
        "accent_press": base.darker(112).name(),
    }


def stylesheet() -> str:
    """Den lille QSS der supplerer den native stil."""
    values = dict(C)
    values.update(_accent_variants())
    return _QSS % values


# Registrerede kald der skal koeres naar farvetemaet skifter. Widgets kan ikke
# blot lytte paa QStyleHints selv: de skal ogsaa have deres cachede ikoner og
# QSS opdateret, og det skal ske i den rigtige raekkefoelge.
_scheme_hooks: list = []

COLOR_SCHEMES = ("system", "light", "dark")


def on_scheme_changed(callback) -> None:
    """Registrér et kald der skal koeres efter et temaskift."""
    if callback not in _scheme_hooks:
        _scheme_hooks.append(callback)


def _scheme_enum(mode: str):
    return {"light": Qt.ColorScheme.Light,
            "dark": Qt.ColorScheme.Dark}.get(mode, Qt.ColorScheme.Unknown)


def set_color_scheme(app: QApplication, mode: str) -> None:
    """Skift mellem ``"system"``, ``"light"`` og ``"dark"``.

    ``Unknown`` betyder "foelg systemet" -- det er Qt's egen konvention, ikke en
    fejlvaerdi. Efter skiftet genlaeses tokens, QSS bygges igen, ikon-cachen
    ryddes (ikonerne er tegnet i tekstfarven) og alle vinduer males om.
    """
    hints = QGuiApplication.styleHints()
    try:
        hints.setColorScheme(_scheme_enum(mode if mode in COLOR_SCHEMES else "system"))
    except Exception as e:  # pragma: no cover - aeldre Qt uden API'et
        logger.warning("Kunne ikke saette farvetema '%s': %s", mode, e)
    refresh(app)


def refresh(app: QApplication) -> None:
    """Genlaes tokens fra den aktuelle palet og mal alt om."""
    sync_tokens_from_palette(app.palette())
    app.setStyleSheet(stylesheet())
    for cb in list(_scheme_hooks):
        try:
            cb()
        except Exception:
            logger.exception("Fejl i tema-hook %r", cb)
    for w in app.topLevelWidgets():
        w.update()


def apply_theme(app: QApplication, mode: str = "system") -> None:
    """Saet stil, font, farvetema og QSS paa hele applikationen.

    Kaldes én gang, lige efter ``QApplication`` er oprettet og foer det foerste
    vindue bygges. Alt er pakket ind: et manglende ``windows11``-tema (aeldre
    Windows, anden platform) maa aldrig forhindre opstart -- Qt falder da
    tilbage paa sin egen standardstil.
    """
    try:
        style = QStyleFactory.create("windows11")
        if style is not None:
            app.setStyle(style)
        else:
            logger.info("Stilen 'windows11' findes ikke; beholder Qt's standard.")
    except Exception as e:  # pragma: no cover - platformafhaengigt
        logger.warning("Kunne ikke saette 'windows11'-stilen: %s", e)

    app.setFont(font("base"))

    hints = QGuiApplication.styleHints()
    try:
        hints.setColorScheme(_scheme_enum(mode))
        # Foelger vi systemet, skal et skift dér slaa igennem med det samme.
        hints.colorSchemeChanged.connect(lambda _s: refresh(app))
    except Exception as e:  # pragma: no cover
        logger.warning("Farvetema kunne ikke initialiseres: %s", e)

    # Paletten roeres IKKE -- den native er kalibreret til stilens egen tegning.
    # I stedet laeses vores tokens ud af den, saa selvtegnede flader matcher.
    sync_tokens_from_palette(app.palette())
    app.setStyleSheet(stylesheet())


# --------------------------------------------------------------------------
# Smaa byggeklodser
# --------------------------------------------------------------------------
def hairline(orient: str = "horizontal", parent=None) -> QFrame:
    """1 px skillelinje i ``C["border"]``.

    ``QFrame.HLine`` tegner en 2 px indgraveret linje der ser fremmed ud i
    Windows 11's flade sprog. En 1 px farvet ``QFrame`` rammer praecist.
    """
    f = QFrame(parent)
    f.setObjectName("Hairline")
    f.setFrameShape(QFrame.Shape.NoFrame)
    # Uden dette males QSS-baggrunden ikke: en ``QFrame`` tegner kun sin ramme,
    # ikke sin flade, medmindre den beder stilen om at gøre det.
    f.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
    if orient == "vertical":
        f.setFixedWidth(1)
    else:
        f.setFixedHeight(1)
    return f


def muted(label, kind: str = "Muted"):
    """Giv en ``QLabel`` en semantisk farve via QSS-id (Muted/Danger/...)."""
    label.setObjectName(kind)
    label.style().unpolish(label)
    label.style().polish(label)
    return label


def elide(text: str, width: int, fm, mode=Qt.TextElideMode.ElideRight) -> str:
    """Forkort tekst til ``width`` px med Qt's egen maaling.

    Afloeser 8.x' binaersoegning over ``tkfont.measure`` -- ``QFontMetrics``
    kan det direkte og korrekt for kombinerede tegn.
    """
    return fm.elidedText(text, mode, width)
