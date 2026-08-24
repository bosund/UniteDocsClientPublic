"""Designtokens for Unite Docs' brugerflade.

Dette modul er **eneste sandhed** for farve, spacing og font. Ingen anden fil i
``client/app`` maa indeholde et haardkodet ``#RRGGBB``-litteral.

Det er *ikke* en stilmotor. Chromet leveres af **sv-ttk** (Sun Valley, MIT), som
er et *pixmap*-tema: hver ttk-widget tegnes fra faste PNG-sprites, saa
``style.configure("X.TButton", background=...)`` bliver ignoreret uden fejl.
Derfor kan farver kun saettes paa de widgets sv-ttk ikke roerer -- ``tk.Canvas``,
``tk.Text``, ``tk.Frame``, ``tk.Label``, ``tk.Menu`` -- og det er praecis dét
``C`` er til.

Farverne nedenfor er **aflaest** i den installerede ``sv_ttk`` (``theme/light.tcl``
og ``theme/spritesheet_light.png``), ikke opfundet. Ellers ville de raa Tk-flader
ikke matche ttk-chromet, og soemmene ville vaere synlige.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from .logging_config import get_logger

logger = get_logger(__name__)

try:
    import sv_ttk
    _sv_ttk_support = True
except ImportError:  # pragma: no cover - kun i ufuldstaendige installationer
    _sv_ttk_support = False


# --------------------------------------------------------------------------
# Farver
# --------------------------------------------------------------------------
# Kilde (sv_ttk 2.6.1, sun-valley-light):
#   light.tcl        -fg #1c1c1c . -bg #fafafa . -disfg #a0a0a0
#                    -selbg #2f60d8 . -accent #005fb8
#                    Treeview selected #e7e7e7 / #191919 . Menu #e7e7e7
#   spritesheet      card-fyld #fafafa, card-kant #e7e7e7
#                    knap-fyld #fdfdfd, knap-kant #ececec, bundkant #d6d6d6
#                    knap-hover #f9f9f9
# De semantiske farver (danger/success/warning) findes ikke i temaet og er taget
# fra Windows 11's egen palet.
C = {
    "bg":            "#fafafa",   # vinduets flade == sv-ttk -bg
    "bg_chrome":     "#fafafa",   # kommandobar/statuslinje (flugter med bg)
    "bg_sunken":     "#f0f0f0",   # nedsaenket omraade (miniaturegitter)
    "surface":       "#fdfdfd",   # haevet flade: kort, fliser, knap-fyld
    "border":        "#e7e7e7",   # haarstreg / kortkant
    "border_strong": "#d6d6d6",   # tydelig kant (tooltip, knappens bundkant)
    "text":          "#1c1c1c",
    "text_muted":    "#626262",
    "text_disabled": "#a0a0a0",
    "accent":        "#005fb8",
    "accent_subtle": "#eaf2fb",   # accent som lys fyldfarve (markeret flise)
    "selection":     "#2f60d8",   # sv-ttk -selbg (tekstmarkering)
    "selection_fg":  "#ffffff",
    "hover":         "#f9f9f9",
    "danger":        "#c42b1c",
    "success":       "#0f7b0f",
    "warning":       "#9d5d00",
    "link":          "#005fb8",
    "page_gutter":   "#54595f",   # moerk baggrund bag PDF-sider (korrekt for en fremviser)
    "page_gutter_fg":"#d6d6d6",   # lys tekst paa den moerke fremviser-baggrund
    "page_shadow":   "#2b2b2b",
    "paper":         "#ffffff",   # hvidt PDF-papir (sideflade mens den renderes)
    "tile_bg":       "#f0f0f0",   # sideflise i hvile
    "drop_target":   "#005fb8",   # ramme om drop-maalet
    "drop_line":     "#005fb8",   # indsaetnings-indikator
    "redact_bar":    "#000000",   # maskering er altid sort (semantisk, ikke tema)
}

# --------------------------------------------------------------------------
# Spacing (logiske px; koer gennem px() for DPI-skalering)
# --------------------------------------------------------------------------
SPACE = {"xxs": 2, "xs": 4, "sm": 6, "md": 8, "lg": 12, "xl": 16, "xxl": 24}

# --------------------------------------------------------------------------
# Fonte. Segoe UI frem for sv-ttk's "Segoe UI Variable Text", fordi tk-widgets
# (Text/Label/Canvas) skal matche resten af appen og Variable-familien ikke
# findes paa alle understoettede Windows-versioner.
# --------------------------------------------------------------------------
_FAMILY = "Segoe UI"
FONTS = {
    "base":    (_FAMILY, 9),
    "strong":  (_FAMILY, 9, "bold"),
    "small":   (_FAMILY, 8),
    "caption": (_FAMILY, 8),
    "title":   (_FAMILY, 12, "bold"),
    "link":    (_FAMILY, 9, "underline"),
    "italic":  (_FAMILY, 9, "italic"),
}

# Ikonstoerrelser (logiske px). cmdlg = store kommandobar-ikoner (ikon over
# tekst), cmd = kompakt kommandobar, tool = vaerktoejslinje.
ICON = {"cmdlg": 24, "cmd": 20, "tool": 18, "small": 16, "tiny": 14}

TOOLTIP_DELAY_MS = 450

# Raekkehoejde i filtraeet. Miniaturen er pdf_renderer.THUMB_SIZE (90 px), saa 96
# giver luft foroven/forneden.
TREE_ROWHEIGHT = 96


# --------------------------------------------------------------------------
# DPI
# --------------------------------------------------------------------------
def scaling(root) -> float:
    """Skaermens skalering i forhold til 96 dpi, klemt til 1.0-2.0.

    sv-ttk's sprites har fast stoerrelse og skalerer ikke, saa vi holder faktoren
    konservativ indtil DPI-fasen.
    """
    try:
        factor = float(root.winfo_fpixels("1i")) / 96.0
    except (tk.TclError, ValueError, ZeroDivisionError):
        return 1.0
    return max(1.0, min(2.0, factor))


def px(root, n: int) -> int:
    """Skalér et logisk pixeltal til skaermens DPI."""
    return max(1, int(round(n * scaling(root))))


# --------------------------------------------------------------------------
# Tema
# --------------------------------------------------------------------------
def apply_theme(root) -> ttk.Style:
    """Aktivér sun-valley-light og saet de faa ting et pixmap-tema stadig lytter til.

    Returnerer den ``ttk.Style`` kalderen kan gemme. Fejler ``sv_ttk``-importen
    (ufuldstaendig installation, manglende package data i en frossen build), koeres
    appen videre paa det tema den allerede har -- et manglende tema maa aldrig
    forhindre opstart.
    """
    style = ttk.Style(root)

    if _sv_ttk_support:
        try:
            sv_ttk.set_theme("light", root=root)
        except Exception as e:  # tcl-fejl, manglende assets i frossen build
            logger.error("sv-ttk kunne ikke aktiveres (%s); beholder '%s'",
                         e, style.theme_use())
    else:
        logger.warning("sv_ttk ikke installeret; beholder temaet '%s'",
                       style.theme_use())

    # rowheight er en almindelig konfigurationsoption og virker fint under et
    # pixmap-tema (i modsaetning til background/relief).
    style.configure("Treeview", rowheight=px(root, TREE_ROWHEIGHT))

    # Kompakt ikonknap: samme sprites som Toolbutton, men lille padding, saa en
    # taet ikonbar (fx sidevisningens annotationslinje med ~15 knapper) faar plads
    # paa én raekke. padding er en layout-option, saa den honoreres ogsaa under et
    # pixmap-tema.
    style.configure("Compact.Toolbutton", padding=(2, 2, 2, 2))

    # Font-defaults for de klassiske widgets sv-ttk ikke roerer.
    for klass in ("*Text", "*Menu", "*Listbox"):
        try:
            root.option_add(klass + ".font", FONTS["base"])
        except tk.TclError:
            pass

    # NB: root.configure() kan IKKE bruges her. PDFTool har overskrevet .config
    # med sin AppConfig-instans, saa Tk's egen configure() er utilgaengelig via
    # attributten. Tal derfor direkte med Tcl.
    try:
        root.tk.call(str(root), "configure", "-background", C["bg"])
    except tk.TclError as e:
        logger.debug("Kunne ikke saette root-baggrund: %s", e)

    return style


# --------------------------------------------------------------------------
# Smaa byggeklodser
# --------------------------------------------------------------------------
def hairline(parent, orient: str = "vertical", **kw) -> tk.Frame:
    """1 px skillelinje i ``C["border"]``.

    ``ttk.Separator`` duer ogsaa, men en ``tk.Frame`` kan farves praecist og
    fungerer ogsaa inde i ``tk``-containere.
    """
    if orient == "vertical":
        kw.setdefault("width", 1)
    else:
        kw.setdefault("height", 1)
    return tk.Frame(parent, background=C["border"], bd=0, highlightthickness=0, **kw)


def style_text(widget) -> None:
    """Giv en ``tk.Text`` tokenfarver (sv-ttk styler den ikke)."""
    try:
        widget.configure(
            background=C["surface"], foreground=C["text"],
            insertbackground=C["text"],
            selectbackground=C["selection"], selectforeground=C["selection_fg"],
            highlightthickness=1,
            highlightbackground=C["border"], highlightcolor=C["accent"],
            relief="flat", borderwidth=0, font=FONTS["base"],
        )
    except tk.TclError as e:
        logger.debug("style_text fejlede: %s", e)


def style_canvas(widget, bg_key: str = "bg_sunken") -> None:
    """Giv et ``tk.Canvas`` tokenbaggrund uden 3D-kant."""
    try:
        widget.configure(background=C[bg_key], highlightthickness=0,
                         bd=0, relief="flat")
    except tk.TclError as e:
        logger.debug("style_canvas fejlede: %s", e)
