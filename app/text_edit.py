"""Rediger-gruppens motor: Ret tekst, Slet omraade og Indsaet tekst.

GUI-frit (som ``annotations``/``anonymize``) og testes headless. Alle tre
aendrer sidens INDHOLD (ikke annotationer) og materialiseres af :func:`apply`
-- ved gem og i forhaandsvisningen.

**Ret tekst** -- én linje ad gangen, uden ombrydning:

En PDF har ingen afsnit at redigere i, kun tegn placeret paa koordinater. En
rettelse er derfor to ting: de tegn der er aendret **fjernes** (en redaction
uden fyld, der lader billeder og streger staa), og den nye tekst **skrives**
samme sted, i samme stoerrelse og farve.

Kun det der faktisk er aendret roeres. ``plan`` finder det faelles foran- og
bagstykke af den gamle og den nye linje; resten af linjen bliver liggende som
den var tegnet -- med sin egen skrift, knibning og eventuelle fede ord. Har
midterstykket ikke samme bredde som det det erstatter, ville bagstykket staa
forkert, og saa skrives linjen om fra aendringen og ud.

Skriften: den indlejrede genbruges hvis den har ALLE de tegn der skal skrives.
De fleste PDF'er indlejrer kun et delsaet (de tegn dokumentet brugte), og et
delsaet uden "ø" kan ikke skrive "ø". Saa bruges den naermeste af PDF's 14
standardskrifter (``fallback``), og kalderen faar det at vide via
``Plan.font_replaced`` -- ellers ligner det en fejl.

Geometri er A-space (uroterede kildesidepunkter, y ned) som al anden
annotationsgeometri; ``apply`` neutraliserer ``/Rotate`` mens den arbejder.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from itertools import groupby

import pymupdf

from . import edit_model as em
from .logging_config import get_logger

logger = get_logger(__name__)

ANNOT_TEXTEDIT = "textedit"
#: Slet omraade: tekst, billedpixels og streger der ligger HELT inde i rammen.
ANNOT_ERASE = "erase"
#: Indsat tekst -- bagt ind i siden som indhold, ikke en annotation.
ANNOT_INSERT_TEXT = "inserttext"
#: Alt Rediger-gruppen laver. Disse aendrer sidens INDHOLD og bages derfor
#: ind i forhaandsvisningen (page_render.content_edits_of).
CONTENT_KINDS = (ANNOT_TEXTEDIT, ANNOT_ERASE, ANNOT_INSERT_TEXT)

#: Stoerrelse paa indsat tekst naar siden ingen tekst har at tage stil fra.
DEFAULT_SIZE = 11.0

#: Laengere tekst presses hoejst saa meget sammen, foer den faar lov at loebe
#: ud over sin plads. Under ~85 % ser bogstaverne tydeligt forvredne ud.
MIN_SCALE = 0.85

# Base14-koder: (familie, fed, kursiv) -> kode til pymupdf.Font(fontname=...)
_BASE14 = {
    ("he", False, False): "helv", ("he", True, False): "hebo",
    ("he", False, True): "heit", ("he", True, True): "hebi",
    ("ti", False, False): "tiro", ("ti", True, False): "tibo",
    ("ti", False, True): "tiit", ("ti", True, True): "tibi",
    ("co", False, False): "cour", ("co", True, False): "cobo",
    ("co", False, True): "coit", ("co", True, True): "cobi",
}
FALLBACK_NAMES = {"he": "Helvetica", "ti": "Times", "co": "Courier"}
_STANDARD = tuple(FALLBACK_NAMES.values())

_SANS_HINTS = ("sans", "arial", "helvetica", "tahoma", "verdana", "calibri",
               "segoe", "frutiger", "univers", "gill", "futura", "roboto",
               "open", "lato", "trebuchet", "century gothic")
_SERIF_HINTS = ("times", "serif", "georgia", "garamond", "cambria", "roman",
                "minion", "palatino", "antiqua", "bookman", "baskerville")
_MONO_HINTS = ("courier", "mono", "consol", "menlo", "lucida console")
_BOLD_HINTS = ("bold", "black", "heavy", "semibold", "demi")

# rawdict med ligaturer udfoldet: "ﬁ" bliver til "f" + "i", saa linjens tekst
# er den brugeren ville skrive, og diff'en mod den nye tekst passer.
_TEXT_FLAGS = pymupdf.TEXTFLAGS_RAWDICT & ~pymupdf.TEXT_PRESERVE_LIGATURES

REASON_ROTATED = "rotated"
REASON_TYPE3 = "type3"


@dataclass(frozen=True)
class Line:
    """En tekstlinje som den staar i kilden."""

    rect: tuple            # (x0, y0, x1, y1) A-space
    text: str
    chars: tuple           # ((c, bbox, origin, span_index), ...)
    spans: tuple           # ((font, size, color_int, flags), ...)
    limit_x: float         # hvor langt mod hoejre en laengere tekst maa gaa
    reason: str = ""       # "" = kan rettes; ellers REASON_*

    @property
    def editable(self) -> bool:
        return not self.reason and bool(self.chars)


@dataclass(frozen=True)
class Plan:
    """Resultatet af :func:`plan`: en faerdig spec plus hvad brugeren skal vide."""

    spec: em.AnnotationSpec
    font_replaced: bool    # den indlejrede skrift manglede tegn
    fallback_name: str     # "Helvetica" | "Times" | "Courier"
    overflow: bool         # teksten gaar ud over sin plads trods sammenpresning


# --------------------------------------------------------------------- linjer
def _strip_subset(basefont: str) -> str:
    """"BCDEEE+Tahoma" -> "Tahoma". Spans navngiver skriften UDEN praefikset."""
    head, sep, tail = basefont.partition("+")
    return tail if sep and len(head) == 6 and head.isupper() else basefont


def lines_on_page(page) -> list:
    """Sidens tekstlinjer i A-space, i den raekkefoelge MuPDF finder dem."""
    type3 = {_strip_subset(f[3]) for f in page.get_fonts(full=True)
             if f[2] == "Type3"}
    raw = page.get_text("rawdict", flags=_TEXT_FLAGS)
    out = []
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            chars, spans = [], []
            for span in line.get("spans", []):
                si = len(spans)
                spans.append((span.get("font", ""), float(span.get("size", 0.0)),
                              int(span.get("color", 0)), int(span.get("flags", 0))))
                for ch in span.get("chars", []):
                    chars.append((ch["c"], tuple(ch["bbox"]), tuple(ch["origin"]), si))
            if not chars or not "".join(c[0] for c in chars).strip():
                continue
            dx, dy = line.get("dir", (1.0, 0.0))
            reason = ""
            if abs(dy) > 1e-3 or dx < 0 or line.get("wmode", 0):
                reason = REASON_ROTATED
            elif any(s[0] in type3 for s in spans):
                reason = REASON_TYPE3
            out.append(dict(rect=tuple(line["bbox"]),
                            text="".join(c[0] for c in chars),
                            chars=tuple(chars), spans=tuple(spans), reason=reason))

    # limit_x: naeste tekst til hoejre paa samme hoejde, ellers tekstspaltens
    # hoejre kant (men aldrig smallere end linjen selv).
    column_right = max((d["rect"][2] for d in out), default=page.rect.width)
    lines = []
    for d in out:
        x0, y0, x1, y1 = d["rect"]
        h = max(1e-3, y1 - y0)
        limit = max(x1, column_right)
        for o in out:
            if o is d:
                continue
            ox0, oy0, _ox1, oy1 = o["rect"]
            overlap = min(y1, oy1) - max(y0, oy0)
            if ox0 >= x1 - 0.5 and overlap > 0.5 * min(h, oy1 - oy0):
                limit = min(limit, ox0 - 1.0)
        lines.append(Line(limit_x=max(x1, limit), **d))
    return lines


def line_at(lines, x: float, y: float, pad: float = 1.5):
    """Linjen der rammes af punktet (A-space), eller None."""
    best = None
    for ln in lines:
        x0, y0, x1, y1 = ln.rect
        if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
            # Overlappende linjer (taet linjeafstand): den hvis midte er naermest.
            d = abs((y0 + y1) / 2.0 - y)
            if best is None or d < best[0]:
                best = (d, ln)
    return best[1] if best else None


def find_edit(specs, line):
    """Den eksisterende rettelse paa ``line`` blandt sidens specs, eller None."""
    for s in specs:
        if s.kind == ANNOT_TEXTEDIT and s.edit is not None:
            if all(abs(a - b) < 0.5 for a, b in zip(s.edit.line_rect, line.rect)):
                return s
    return None


# ------------------------------------------------------------------- skrifter
def fallback_code(fontname: str, flags: int) -> str:
    """Den naermeste Base14-skrift. Navnet vejer tungere end flagene, som mange
    producenter saetter forkert (MuPDF udleder "serif" naar intet andet vides)."""
    n = fontname.lower()
    bold = bool(flags & 16) or any(k in n for k in _BOLD_HINTS)
    italic = bool(flags & 2) or "italic" in n or "oblique" in n
    if any(k in n for k in _MONO_HINTS):
        family = "co"
    elif any(k in n for k in _SANS_HINTS):
        family = "he"
    elif any(k in n for k in _SERIF_HINTS):
        family = "ti"
    elif flags & 8:
        family = "co"
    elif flags & 4:
        family = "ti"
    else:
        family = "he"
    return _BASE14[(family, bold, italic)]


def _has_ink(font, c: str, cache: dict) -> bool:
    """Tegner skriften faktisk noget for ``c``? Proevetegnes paa en lille side."""
    key = ("ink", id(font), c)
    if key not in cache:
        tmp = pymupdf.open()
        try:
            pg = tmp.new_page(width=60, height=60)
            tw = pymupdf.TextWriter(pg.rect)
            tw.append((8, 44), c, font=font, fontsize=40)
            tw.write_text(pg)
            pix = pg.get_pixmap(dpi=72, colorspace=pymupdf.csGRAY)
            cache[key] = min(pix.samples) < 200
        except Exception:
            cache[key] = False
        finally:
            tmp.close()
    return cache[key]


def _covers(font, text: str, seen, cache: dict) -> bool:
    """Kan ``font`` skrive ``text``?

    ``has_glyph`` alene er IKKE nok: MuPDF's delsaetning beholder hele
    tegntabellen men fjerner tegnene, saa et delsaet uden "ø" svarer ja til
    "ø" -- og tegner intet, med en tilfaeldig bredde. Maalt paa
    ``Document.subset_fonts()``. Derfor: tegn der allerede staar paa siden i
    denne skrift (``seen``) er sikre; andre proevetegnes. Et mellemrum kan ikke
    proevetegnes, saa det godkendes kun hvis det er set -- ellers ville
    bredden, og dermed placeringen af ordet efter, vaere gaet.

    ``has_glyph`` kraeves dog ALTID -- ogsaa for sete tegn. ``TextWriter`` slaar
    op via skriftens Unicode-tegntabel, og en skrift uden én (CID-delsaet fra
    InDesign/Acrobat) skriver intet, selv om siden viser tegnet. Og
    :func:`apply` (``seen=None``) kraever det ogsaa: var det kun planen der
    sprang det over, ville planen maale bredder med én skrift og gem skrive
    med en anden -- bagstykket ville staa forkert, uden advarsel.

    ``seen=None`` (ved anvendelse; valget er truffet i :func:`plan`) nøjes med
    ``has_glyph``."""
    for c in set(text):
        if not font.has_glyph(ord(c)):
            return False
        if seen is None or c in seen:
            continue
        if c.isspace() or not _has_ink(font, c, cache):
            return False
    return True


def _seen_chars(page, name: str) -> set:
    """Tegnene siden allerede skriver med skriften ``name``."""
    seen = set()
    raw = page.get_text("rawdict", flags=_TEXT_FLAGS)
    for block in raw.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                if span.get("font") == name:
                    seen.update(ch["c"] for ch in span.get("chars", []))
    return seen


def _embedded(page, basefont: str, name: str, needed: str, cache: dict,
              seen=None):
    """(basefont, Font) for en indlejret skrift der kan skrive ``needed``.

    ``basefont`` (med delsaets-praefiks) foretraekkes; ellers proeves enhver
    indlejret skrift med samme navn uden praefiks -- ved gem er siden kopieret
    ind i et nyt dokument, men /BaseFont-navnene foelger med."""
    doc = page.parent
    for f in page.get_fonts(full=True):
        xref, ext, ftype, fbase = f[0], f[1], f[2], f[3]
        if ftype == "Type3" or ext == "n/a":
            continue
        if basefont and fbase != basefont:
            continue
        if not basefont and _strip_subset(fbase) != name:
            continue
        key = (id(doc), xref)
        if key not in cache:
            font = None
            try:
                _n, _e, _t, buf = doc.extract_font(xref)
                if buf:
                    font = pymupdf.Font(fontbuffer=buf)
            except Exception as e:
                logger.debug("extract_font(%s) fejlede: %s", xref, e)
            cache[key] = font
        font = cache[key]
        if font is not None and _covers(font, needed, seen, cache):
            return fbase, font
    return None


def _base14(code: str, cache: dict):
    key = ("base14", code)
    if key not in cache:
        cache[key] = pymupdf.Font(fontname=code)
    return cache[key]


# ---------------------------------------------------------------------- plan
def _common_ends(old: str, new: str) -> tuple:
    n, m = len(old), len(new)
    p = 0
    while p < min(n, m) and old[p] == new[p]:
        p += 1
    s = 0
    while s < min(n, m) - p and old[n - 1 - s] == new[m - 1 - s]:
        s += 1
    return p, s


def _is_ligature_tail(chars, i: int) -> bool:
    """Er ``chars[i]`` en hale i en udfoldet ligatur -- samme glyf som tegnet
    foran?

    Maalt: med ligaturerne udfoldet faar "ﬁ" tegnene ``f`` (62-74.56) og ``i``
    (74.56-74.56). Foerste bogstav baerer hele glyffens ramme; resten har
    bredde nul og sidder ved glyffens hoejre kant."""
    if i <= 0 or i >= len(chars):
        return False
    b, prev = chars[i][1], chars[i - 1][1]
    return (b[2] - b[0]) < 0.01 and abs(b[0] - prev[2]) < 0.01


def _glyph_bounds(chars, a: int, b: int) -> tuple:
    """Udvid ``[a, b)`` til hele glyffer. En PDF-ligatur er ÉN glyf, og MuPDF
    fjerner hele glyffen saa snart den rammes -- et "i" i "ﬁ" kan ikke slettes
    uden ogsaa at slette "f". Derfor skal hele ligaturen med og skrives igen."""
    while a > 0 and _is_ligature_tail(chars, a):
        a -= 1
    while b < len(chars) and _is_ligature_tail(chars, b):
        b += 1
    return a, b


def _redact_rect(chars, a: int, b: int) -> tuple:
    """Et rektangel der rammer tegnene ``chars[a:b]`` og IKKE naboerne.

    MuPDF fjerner et tegn hvis dets ramme overlapper rektanglet det mindste
    stykke. Derfor gaar rektanglet fra midten af det foerste tegn til midten af
    det sidste (lidt ud til hver side), og kun i linjens midterbaand: naboernes
    rammer roerer typisk kanten af yderste tegn, og naboLINJERNES rammer kan
    overlappe ved taet linjeafstand.

    Ligatur-haler (bredde nul ved glyffens hoejre kant) springes over som
    yderpunkter: midten af en hale ER naboens venstre kant."""
    wide = [i for i in range(a, b) if not _is_ligature_tail(chars, i)] or [a, b - 1]
    c0, c1 = chars[wide[0]][1], chars[wide[-1]][1]
    w0 = max(0.4, c0[2] - c0[0])
    w1 = max(0.4, c1[2] - c1[0])
    x0 = (c0[0] + c0[2]) / 2.0 - 0.2 * w0
    x1 = (c1[0] + c1[2]) / 2.0 + 0.2 * w1
    ys = [v for c in chars[a:b] for v in (c[1][1], c[1][3])]
    cy = (min(ys) + max(ys)) / 2.0
    band = max(0.3, 0.2 * (max(ys) - min(ys)))
    return (x0, cy - band, x1, cy + band)


def plan(page, line: Line, new_text: str):
    """Regn en rettelse af ``line`` til ``new_text`` ud. None = ingen aendring.

    ``page`` er kildesiden (skrifterne slaas op i dens ressourcer)."""
    old = line.text
    if new_text == old or not line.editable:
        return None
    chars = line.chars
    n, m = len(old), len(new_text)
    p, s = _common_ends(old, new_text)
    # Rammer aendringen en ligatur, skal hele glyffen med (se _glyph_bounds).
    # Teksterne er ens i for- og bagstykket, saa de udvides lige meget.
    p2, end2 = _glyph_bounds(chars, p, n - s)
    p, s = p2, n - end2
    s = min(s, m - p)
    style = chars[min(p, n - 1)]
    fname, size, color_int, flags = line.spans[style[3]]
    start_x = style[2][0] if p < n else chars[-1][1][2]
    baseline = style[2][1]
    code = fallback_code(fname, flags)
    cache: dict = {}
    seen = _seen_chars(page, fname)

    def resolve(text):
        hit = _embedded(page, "", fname, text, cache, seen)
        if hit:
            return hit[0], hit[1], False
        # Er kilden selv en standardskrift (Helvetica, Times, Courier), er
        # reserven den samme skrift -- det er ikke en udskiftning brugeren skal
        # advares om. Og skrives der intet (slutningen af linjen er slettet),
        # er der heller ingen skrift at udskifte.
        replaced = bool(text.strip()) and not fname.startswith(_STANDARD)
        return "", _base14(code, cache), replaced

    middle = new_text[p:len(new_text) - s]
    basefont, font, replaced = resolve(middle)
    if s:
        old_w = chars[n - s][2][0] - start_x
        new_w = font.text_length(middle, fontsize=size)
        if abs(new_w - old_w) > max(0.5, 0.04 * size):
            # Bagstykket ville staa forkert: skriv linjen om fra aendringen.
            s = 0
            middle = new_text[p:]
            basefont, font, replaced = resolve(middle)

    end = n - s
    rects = (_redact_rect(chars, p, end),) if end > p else ()
    width = font.text_length(middle, fontsize=size) if middle else 0.0
    scale, overflow = 1.0, False
    room = line.limit_x - start_x
    if width > room + 0.5 and width > 0:
        scale = max(MIN_SCALE, room / width)
        overflow = width * scale > room + 0.5

    spec = em.AnnotationSpec(
        kind=ANNOT_TEXTEDIT, rects=rects, text=new_text,
        color=tuple(pymupdf.sRGB_to_pdf(color_int)), fontsize=size,
        edit=em.TextEdit(old_text=old, line_rect=tuple(line.rect), insert=middle,
                         origin=(start_x, baseline), font=basefont,
                         fallback=code, scale=round(scale, 4)))
    return Plan(spec=spec, font_replaced=replaced,
                fallback_name=FALLBACK_NAMES[code[:2]], overflow=overflow)


# ---------------------------------------------------------------- indsaet
def nearest_line(lines, x: float, y: float):
    """Linjen hvis stil indsat tekst skal arve: den lodret naermeste, med et
    mindre tillaeg for vandret afstand (en linje i naboklummen taber til én
    laengere oppe i samme klumme)."""
    best = None
    for ln in lines:
        if ln.reason:
            continue
        x0, y0, x1, y1 = ln.rect
        dy = abs((y0 + y1) / 2.0 - y)
        dx = x0 - x if x < x0 else (x - x1 if x > x1 else 0.0)
        d = dy + 0.25 * dx
        if best is None or d < best[0]:
            best = (d, ln)
    return best[1] if best else None


def plan_insert(page, lines, x: float, y: float, text: str, angle: int = 0,
                origin=None):
    """Indsat tekst ved klikket (x, y) i A-space. None hvis teksten er tom.

    Stoerrelse, farve og skrift arves fra den naermeste linje -- og den
    indlejrede skrift genbruges paa samme vilkaar som ved Ret tekst. ``angle``
    er sidens samlede visningsrotation: teksten drejes med den, saa den staar
    vandret paa skaermen (maalt: ``Matrix(angle)`` som morph giver vandret
    tekst ved alle fire /Rotate-vaerdier). Uden ``origin`` lander klikket midt
    paa tekstens x-hoejde; ved genredigering genbruges den gamle grundlinje."""
    if not text.strip():
        return None
    ref = nearest_line(lines, x, y)
    cache: dict = {}
    if ref is not None:
        counts = {}
        for ch in ref.chars:
            counts[ch[3]] = counts.get(ch[3], 0) + 1
        fname, size, color_int, flags = ref.spans[max(counts, key=counts.get)]
        hit = _embedded(page, "", fname, text, cache, _seen_chars(page, fname))
    else:
        fname, size, color_int, flags, hit = "Helvetica", DEFAULT_SIZE, 0, 0, None
    code = fallback_code(fname, flags)
    font = hit[1] if hit else _base14(code, cache)
    replaced = ref is not None and hit is None and not fname.startswith(_STANDARD)

    rot = pymupdf.Matrix(angle)
    if origin is None:
        o = pymupdf.Point(x, y) + pymupdf.Point(0, 0.35 * size) * rot
    else:
        o = pymupdf.Point(*origin)
    width = font.text_length(text, fontsize=size)
    box = pymupdf.Rect(o.x, o.y - 0.8 * size, o.x + width, o.y + 0.25 * size)
    if angle % 360:
        box = box.morph(o, rot).rect
    spec = em.AnnotationSpec(
        kind=ANNOT_INSERT_TEXT, text=text,
        color=tuple(pymupdf.sRGB_to_pdf(color_int)), fontsize=size,
        edit=em.TextEdit(old_text="", line_rect=tuple(box), insert=text,
                         origin=(o.x, o.y), font=hit[0] if hit else "",
                         fallback=code, angle=int(angle) % 360))
    return Plan(spec=spec, font_replaced=replaced,
                fallback_name=FALLBACK_NAMES[code[:2]], overflow=False)


def insert_at(specs, x: float, y: float, pad: float = 2.0):
    """Den indsatte tekst der rammes af punktet (A-space), eller None."""
    for s in reversed(tuple(specs)):
        if s.kind == ANNOT_INSERT_TEXT and s.edit is not None:
            x0, y0, x1, y1 = s.edit.line_rect
            if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                return s
    return None


# --------------------------------------------------------------------- apply
def _stash_foreign_redactions(page) -> list:
    """Tag kildefilens EGNE, ikke-udfoerte maskeringsmarkeringer af siden.

    ``apply_redactions()`` udfoerer alle Redact-annotationer paa siden -- ikke
    kun dem vi lige har lagt paa. En PDF med "markeret til maskering" fra
    Acrobat ville ellers faa indhold fjernet permanent af en tekstrettelse,
    uden at brugeren nogensinde blev spurgt. Laeg dem tilbage med
    :func:`_restore_foreign_redactions` (de er stadig kun markeringer)."""
    saved = []
    for annot in list(page.annots(types=[pymupdf.PDF_ANNOT_REDACT]) or []):
        try:
            verts = annot.vertices or []
            quads = [pymupdf.Quad(*verts[i:i + 4]) for i in range(0, len(verts) - 3, 4)]
            saved.append({"areas": quads or [annot.rect],
                          "fill": (annot.colors or {}).get("fill"),
                          "info": dict(annot.info)})
            page.delete_annot(annot)
        except Exception as e:
            logger.warning("Kunne ikke parkere en maskeringsmarkering: %s", e)
    return saved


def _restore_foreign_redactions(page, saved) -> None:
    for item in saved:
        try:
            for area in item["areas"]:
                kw = {"fill": item["fill"]} if item["fill"] else {}
                annot = page.add_redact_annot(area, **kw)
                info = {k: v for k, v in item["info"].items()
                        if k in ("title", "content", "subject") and v}
                if info:
                    annot.set_info(**info)
                annot.update()
        except Exception as e:
            logger.warning("Kunne ikke genskabe en maskeringsmarkering: %s", e)


def apply_own_redactions(page, rects, fill=False, **options) -> None:
    """``apply_redactions`` for KUN vores egne rektangler -- kildefilens
    markeringer parkeres imens og laegges tilbage (se
    :func:`_stash_foreign_redactions`). Bruges ogsaa af Masker.

    ``rects`` er rektangler, eller ``(rektangel, fyld)``-par naar fyldet
    varierer (Masker). ``fill=False`` er ingen fyld."""
    if not rects:
        return
    saved = _stash_foreign_redactions(page)
    try:
        for item in rects:
            r, f = item if len(item) == 2 else (item, fill)
            page.add_redact_annot(pymupdf.Rect(*r), fill=f)
        page.apply_redactions(**options)
    finally:
        _restore_foreign_redactions(page, saved)


def apply(page, specs) -> None:
    """Materialiser Rediger-gruppens rettelser paa en levende side (gem og
    forhaandsvisning).

    Rettelserne anvendes i den raekkefoelge de blev lavet, i hold af samme
    slags: et viskelaeder over en rettet linje skal fjerne den rettede tekst,
    og tekst skrevet hvor man lige har visket ud, skal blive staaende.
    ``apply_redactions`` er sidedaekkende, saa hvert hold er eet kald med sine
    egne indstillinger:

    * Slet omraade: billedpixels blankes (hvide -- maalt), og kun streger der
      ligger HELT inde i rammen fjernes (``REMOVE_IF_COVERED``, maalt: en
      baggrund der stikker ud, bliver staaende).
    * Ret tekst: billeder og streger roeres slet ikke.

    Alt det her koerer FOER maskeringen, som har sine egne, haardere
    indstillinger -- og fordi maskeringen kommer bagefter, bliver indhold der
    ogsaa er maskeret, fjernet."""
    specs = [s for s in specs if s.kind == ANNOT_ERASE
             or (s.kind in (ANNOT_TEXTEDIT, ANNOT_INSERT_TEXT) and s.edit is not None)]
    if not specs:
        return
    original_rot = page.rotation
    cache: dict = {}
    try:
        if original_rot:
            page.set_rotation(0)
        for is_erase, group in groupby(specs, key=lambda s: s.kind == ANNOT_ERASE):
            group = list(group)
            rects = [r for s in group if s.kind != ANNOT_INSERT_TEXT for r in s.rects]
            if is_erase:
                apply_own_redactions(
                    page, rects, images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            else:
                apply_own_redactions(
                    page, rects, images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE)
            if is_erase:
                continue
            for s in group:
                try:
                    _write(page, s, cache)
                except Exception as e:
                    logger.warning("Kunne ikke skrive tekst (%s): %s", s.kind, e)
    finally:
        if original_rot:
            page.set_rotation(original_rot)


def _write(page, s, cache) -> None:
    e = s.edit
    if not e.insert:
        return
    font = None
    if e.font:
        hit = _embedded(page, e.font, "", e.insert, cache)
        if hit:
            font = hit[1]
    if font is None:
        font = _base14(e.fallback or "helv", cache)
    origin = pymupdf.Point(*e.origin)
    tw = pymupdf.TextWriter(page.rect, color=tuple(s.color))
    # Ord for ord, sat paa den maalte position -- mellemrummene skrives ikke.
    # Et delsaet afbilder ofte U+0020 og U+00A0 paa samme glyf, og MuPDF's
    # ToUnicode vaelger saa den sidste: teksten blev udtrukket med haarde
    # mellemrum, og soegning/kopiering i den gemte fil holdt op med at virke.
    pos = 0
    for token in re.split(r"(\s+)", e.insert):
        if token and not token.isspace():
            dx = font.text_length(e.insert[:pos], fontsize=s.fontsize)
            tw.append(pymupdf.Point(origin.x + dx, origin.y), token,
                      font=font, fontsize=s.fontsize)
        pos += len(token)
    if not tw.text_rect or tw.text_rect.is_empty:
        return
    # Sammenpresning og drejning om grundlinjens start -- ordene er sat ud fra
    # den, saa de foelger med.
    morph = None
    if e.scale < 0.9999 or e.angle % 360:
        morph = (origin, pymupdf.Matrix(e.scale, 1.0) * pymupdf.Matrix(e.angle))
    tw.write_text(page, morph=morph)
