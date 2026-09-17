"""Automatisk anonymisering — orkestrering mellem model, sider og maskeringer.

Modulet er **GUI-frit**. Det ejer:

* :class:`Finding` / :class:`ScanResult` — ren data der kan krydse traadgraensen
  gennem ``app._queue``. Ingen ``pymupdf``-objekter og ingen widgets slipper ud.
* :func:`scan` — selve gennemloebet af siderne.
* :class:`AnonymizeSession` — tilstanden der goer anonymisering til en *session*
  frem for en engangskoersel, saa en fil der tilfoejes bagefter ikke smutter
  umaskeret igennem.
* :func:`group_findings` / :func:`build_items` — vejen fra fund til
  ``AnnotationSpec``.

Laasedisciplin
--------------
``pdf_renderer.PDF_LOCK`` tages **pr. side**, ikke pr. fil. OCR af en enkelt side
tager sekunder, og et helt bundt kan tage minutter; holdt over hele filen ville
laasen fryse al miniature-optegning imens. Analysen efter udtraekket er ren
Python og koerer uden for laasen.
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field

from . import annotations as an
from . import edit_model as em
from . import langdetect
from . import ocr_text
from . import pii
from .localization import LocalizationManager
from .logging_config import get_logger

# ``pdf_renderer`` traekker ``theme`` og dermed Qt ind. Modulet skal kunne
# importeres GUI-frit (hovedsuiten i ``tests/test_suite.py`` koerer uden skaerm),
# saa importen sker foerst naar en side rent faktisk skal laeses.

_ = LocalizationManager.get_text
logger = get_logger(__name__)

#: Hvor meget tekst der vises som eksempel i gennemgangsdialogen.
CONTEXT_CHARS = 60

#: Hvor langt tilbage vi kigger efter et rolleord foran et navn.
_ROLE_LOOKBEHIND = 80
_ROLE_LOOKAHEAD = 30

def _dk_variants(word: str) -> str:
    """Tillad ae/oe/aa hvor der staar æ/ø/å.

    Tesseract laeser jaevnligt de danske bogstaver forkert, og aeldre dokumenter
    translittererer dem. Et rolleord der ikke matcher, koster et forkert hint
    paa netop de navne hintet er til for.
    """
    return (word.replace("æ", "(?:æ|ae)").replace("ø", "(?:ø|oe)")
                .replace("å", "(?:å|aa)"))


def _role_re(words) -> "re.Pattern":
    return re.compile(r"\b(" + "|".join(_dk_variants(w) for w in words) + r")\b",
                      re.IGNORECASE)


#: Roller hvor navnet typisk skal **bevares** — advokaten og dommeren i en
#: afgoerelse skal netop staa der.
ROLE_PROFESSIONAL = _role_re([
    r"advokatfuldmægtig", r"advokaten", r"advokat", r"adv\.",
    r"beskikket forsvarer", r"forsvareren", r"forsvarer", r"anklageren",
    r"anklager", r"retsformanden", r"retsformand", r"dommeren", r"dommer",
    r"domsmændene", r"domsmand", r"nævning", r"bisidder", r"kurator",
    r"protokolfører", r"retsassessor", r"byretsdommer", r"landsdommer",
    r"sagsbehandler"])

#: Roller hvor navnet typisk **skal** maskeres.
ROLE_PARTY = _role_re([
    r"sagsøgeren", r"sagsøger", r"sagsøgte", r"tiltalte", r"sigtede", r"vidnet",
    r"vidne", r"forurettede", r"klageren", r"klager", r"indklagede",
    r"rekvirenten", r"rekvirent", r"rekvisitus", r"skyldneren", r"skyldner",
    r"kreditor", r"ansøgeren", r"ansøger"])

#: Vises i **Rolle**-kolonnen naar en gruppes forekomster er uenige.
ROLE_MIXED = "blandet"


@dataclass(frozen=True)
class Finding:
    """Én forekomst af en personoplysning paa én side."""

    page_uid: str
    entity: str                 # pii.CPR, pii.PERSON, ...
    value: str                  # teksten som den staar i dokumentet
    rects: tuple                # ((x0, y0, x1, y1), ...) i A-space
    context: str                # tekst omkring hittet, til dialogen
    page_label: str             # "Alfa.pdf · side 3"
    score: float = 0.0
    role_hint: str = ""         # "advokat" | "tiltalte" | ""
    key: str = ""               # normaliseret gruppenoegle
    uid: str = ""               # stabil id, saa dialogen kan af-/vaelge


@dataclass(frozen=True)
class ScanResult:
    findings: tuple = ()
    pages_scanned: int = 0
    pages_ocred: int = 0
    pages_failed: int = 0
    cancelled: bool = False
    #: Sider hvor der var bevis for engelsk, og den engelske model derfor koerte
    #: oven i den danske. Vises i gennemgangsdialogen sammen med OCR-tallet.
    pages_english: int = 0
    #: Sider der saa engelske ud, men hvor den engelske model **manglede**. Deres
    #: navne er fundet af den danske model alene, og den finder maalt 12 af 18.
    pages_english_missing: int = 0


@dataclass
class AnonymizeSession:
    """Anonymiseringens tilstand for det aabne dokument.

    Lever saa laenge vinduet goer og gemmes **ikke** i ``config.ini``: den hoerer
    til den aabne filliste, ikke til brugerens indstillinger.
    """

    has_run: bool = False
    #: Slaas fra af "Ikke flere filer"; et nyt tryk paa knappen slaar den til igen.
    prompt_on_new_files: bool = True
    scanned_page_uids: set = field(default_factory=set)
    #: Gruppenoegle -> skal maskeres. Baerer brugerens fravalg videre, saa anden
    #: koersel ikke maskerer dét foerste koersel bevidst lod staa.
    decisions: dict = field(default_factory=dict)

    def unscanned(self, model) -> list:
        """Side-uid'er der endnu ikke har vaeret igennem en scanning."""
        return [p.uid for _f, p in model.flatten()
                if p.uid not in self.scanned_page_uids]

    def mark_scanned(self, uids) -> None:
        self.scanned_page_uids.update(uids)

    def remember(self, groups, selected_uids) -> None:
        """Husk hvilke grupper brugeren valgte fra."""
        for g in groups:
            self.decisions[g.key] = any(f.uid in selected_uids for f in g.findings)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------

def build_jobs(model, only_uids=None) -> list:
    """Snapshot af siderne der skal scannes. **Kaldes paa UI-traaden.**

    Returnerer rene tupler ``(page_uid, src_path, kind, src_index, rotation,
    page_label)``. Ingen model- eller widget-objekter krydser traadgraensen.
    Sorteret efter kildefil, saa hver fil aabnes én gang.
    """
    wanted = set(only_uids) if only_uids is not None else None
    jobs = []
    for entry in model.files:
        name = os.path.basename(entry.path)
        for n, page in enumerate(entry.pages, start=1):
            if wanted is not None and page.uid not in wanted:
                continue
            label = _("%(fil)s · side %(nr)d") % {"fil": name, "nr": n}
            jobs.append((page.uid, page.src_path, em.kind_for_path(page.src_path),
                         page.src_index, page.rotation, label))
    jobs.sort(key=lambda j: (os.path.normcase(j[1]), j[3]))
    return jobs


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _context_for(text: str, start: int, end: int) -> str:
    lo = max(0, start - CONTEXT_CHARS)
    hi = min(len(text), end + CONTEXT_CHARS)
    snippet = text[lo:hi].replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    return ("…" if lo > 0 else "") + snippet + ("…" if hi < len(text) else "")


def role_hint(text: str, start: int, end: int) -> str:
    """Rollen der staar naermest foran (eller lige efter) et navn.

    Rent vejledende: fundet forbliver afkrydset. Men i en afgoerelse er det
    forskellen paa advokatens navn — der skal blive staaende — og partens.
    """
    before = text[max(0, start - _ROLE_LOOKBEHIND):start]
    # Kun samme afsnit: et rolleord paa den anden side af et linjeskift hoerer
    # typisk til en anden person.
    cut = before.rfind("\n")
    if cut != -1:
        before = before[cut + 1:]

    best = ""
    best_pos = -1
    for rx in (ROLE_PROFESSIONAL, ROLE_PARTY):
        for m in rx.finditer(before):
            if m.start() > best_pos:
                best, best_pos = m.group(1).lower(), m.start()
    if best:
        return best

    # Efterstillet form ("Anne Hansen, advokat") — men KUN umiddelbart efter.
    # Et bredt kig fremad ville tage rollen fra den *naeste* person i saetningen.
    after = text[end:end + _ROLE_LOOKAHEAD]
    for rx in (ROLE_PROFESSIONAL, ROLE_PARTY):
        m = rx.search(after)
        if m and m.start() <= 2:
            return m.group(1).lower()
    return ""


def _page_words(path, kind, src_index, rotation, passwords, cancel):
    """``(ord, brugte_ocr)`` for én side. Kalderen skal holde ``PDF_LOCK``."""
    from . import pdf_renderer
    if kind == em.KIND_IMAGE:
        words = ocr_text.image_words_a_space(path, rotation_hint=rotation,
                                             cancel=cancel)
        return words, bool(words)

    doc = pdf_renderer._doc_cache.get_or_open(path, passwords)
    if doc is None or src_index >= doc.page_count:
        return [], False
    page = doc[src_index]
    used_ocr = ocr_text.needs_ocr(page)
    words = ocr_text.page_words_a_space(
        page, rotation_hint=rotation, cancel=cancel,
        cache_path=path, cache_index=src_index)
    return words, used_ocr


def scan(jobs, passwords, *, allow_ocr=True, on_progress=None, cancel=None) -> ScanResult:
    """Gennemloeb ``jobs`` og find personoplysninger. **Arbejdstraad.**"""
    from . import pdf_renderer

    findings = []
    scanned = ocred = failed = english = english_missing = 0
    total = len(jobs)
    # Teksten pr. side beholdes til anden gennemloeb (navne-propagering).
    # Kun teksten -- ordlisterne ville vaere hundredvis af MB paa et stort bundt.
    sider = []

    for i, (page_uid, path, kind, src_index, rotation, label) in enumerate(jobs):
        if cancel is not None and cancel.is_set():
            return ScanResult(tuple(findings), scanned, ocred, failed, True,
                              english, english_missing)
        try:
            # Laasen holdes KUN om MuPDF-arbejdet; analysen nedenfor er ren
            # Python og maa ikke blokere miniature-optegningen.
            with pdf_renderer.PDF_LOCK:
                words, used_ocr = _page_words(path, kind, src_index, rotation,
                                              passwords, cancel)
        except Exception as e:
            logger.warning("Kunne ikke laese tekst fra %s side %s: %s",
                           path, src_index, e)
            failed += 1
            words, used_ocr = [], False

        scanned += 1
        if used_ocr:
            ocred += 1

        if words:
            text, offsets = ocr_text.words_to_text(words)
            sider.append((page_uid, path, kind, src_index, rotation, label, text))
            # Dansk koeres altid; engelsk laegges oven i naar der er bevis for
            # det. Detektionen koster ingen model — se app/langdetect.py.
            is_en = langdetect.looks_english(text)
            if is_en:
                if pii.availability(pii.LANGUAGE_EN)[0]:
                    english += 1
                else:
                    english_missing += 1
            try:
                spans = pii.analyze(text, ocr=used_ocr, english=is_en)
            except pii.ModelMissing:
                raise
            except Exception as e:
                logger.warning("Analysen fejlede paa %s: %s", label, e)
                spans = []
            for s in spans:
                rects = ocr_text.rects_for_span(words, offsets, s.start, s.end)
                if not rects:
                    continue
                value = text[s.start:s.end]
                findings.append(Finding(
                    page_uid=page_uid, entity=s.entity, value=value,
                    rects=tuple(rects), context=_context_for(text, s.start, s.end),
                    page_label=label, score=s.score,
                    role_hint=role_hint(text, s.start, s.end),
                    key=pii.normalize_value(s.entity, value),
                    uid="%s:%d:%d" % (page_uid, s.start, s.end)))

        if on_progress is not None:
            on_progress(i + 1, total, label)

    findings += _propagate_names(findings, sider, passwords, cancel)
    return ScanResult(tuple(findings), scanned, ocred, failed, False,
                      english, english_missing)


# --- navne-propagering -----------------------------------------------------

#: Tokens kortere end dette propageres ikke. ``Bo`` og ``Ry`` er rigtige danske
#: navne, men ogsaa almindelige ord, og et fejlgreb rammer hele dokumentet.
_MIN_NAME_TOKEN = 4

_NAME_SPLIT_RE = re.compile(r"[^\wÆØÅæøå]+", re.UNICODE)


def name_variants(findings) -> dict:
    """Soegemoenstre for navne der allerede er bekraeftet i dokumentet.

    Returnerer ``{moenster: gruppenoegle}``. Moenstrene er dels de enkelte
    navneord, dels **sammenskrivninger** af nabo-ord.

    Sammenskrivningen er ikke teoretisk: paa et rigtigt processkrift laeste OCR
    ``Rasmus Nedergaard`` som ``RasmusNedergaard`` paa to sider ud af otte.
    Modellen ser ét ukendt ord og tagger det ikke, saa navnet slap umaskeret
    igennem netop dér — undermaskering, og usynlig for brugeren.

    Kun **flerleddede** navne propageres. Et enkeltstaaende ord er for ofte et
    fagord: paa samme dokument blev ``Fritvalgslønkonto`` tagget PERSON elleve
    gange, og propagering af den ville have fordoblet stoejen i stedet for at
    fjerne den.
    """
    ejere = {}
    for f in findings:
        if f.entity != pii.PERSON:
            continue
        dele = [d for d in _NAME_SPLIT_RE.split(f.value) if d]
        caps = [d for d in dele
                if d[:1].isupper() and not any(c.isdigit() for c in d)]
        if len(caps) < 2:
            continue
        moenstre = [d for d in caps
                    if len(d) >= _MIN_NAME_TOKEN
                    and d.casefold() not in pii._NAME_STOPWORDS]
        moenstre += [a + b for a, b in zip(caps, caps[1:])]
        for m in moenstre:
            ejere.setdefault(m, set()).add(f.key)
    # Hoerer et ord til flere personer, kan det ikke laegges i den enes gruppe.
    return {m: (next(iter(k)) if len(k) == 1 else pii.normalize_value(pii.PERSON, m))
            for m, k in ejere.items()}


def _propagate_names(findings, sider, passwords, cancel) -> list:
    """Anden gennemloeb: find de kendte navne paa de sider hvor de blev overset."""
    from . import pdf_renderer

    varianter = name_variants(findings)
    if not varianter or not sider:
        return []
    rx = re.compile(r"(" + "|".join(sorted(map(re.escape, varianter),
                                             key=len, reverse=True)) + r")",
                    re.IGNORECASE)
    daekket = {}
    for f in findings:
        daekket.setdefault(f.page_uid, []).append(f)

    ekstra = []
    for (page_uid, path, kind, src_index, rotation, label, text) in sider:
        if cancel is not None and cancel.is_set():
            break
        traef = [m for m in rx.finditer(text)]
        if not traef:
            continue
        # Kun sider med et traef betaler for at faa ordene igen. Cachen i
        # ocr_text goer det gratis for de fleste dokumenter.
        try:
            with pdf_renderer.PDF_LOCK:
                words, _ocr = _page_words(path, kind, src_index, rotation,
                                          passwords, cancel)
        except Exception as e:
            logger.warning("Kunne ikke genlaese %s: %s", label, e)
            continue
        if not words:
            continue
        _txt, offsets = ocr_text.words_to_text(words)
        eksisterende = daekket.get(page_uid, [])
        for m in traef:
            uid = "%s:%d:%d" % (page_uid, m.start(), m.end())
            if any(uid == f.uid for f in eksisterende):
                continue
            rects = ocr_text.rects_for_span(words, offsets, m.start(), m.end())
            if not rects:
                continue
            if any(_rects_overlap(rects, f.rects) for f in eksisterende):
                continue
            ekstra.append(Finding(
                page_uid=page_uid, entity=pii.PERSON, value=m.group(0),
                rects=tuple(rects), context=_context_for(text, m.start(), m.end()),
                page_label=label, score=0.99,
                role_hint=role_hint(text, m.start(), m.end()),
                key=varianter[_variant_key(m.group(0), varianter)],
                uid=uid))
    if ekstra:
        logger.info("Navne-propagering tilfoejede %d fund paa tvaers af siderne",
                    len(ekstra))
    return ekstra


def _variant_key(traf: str, varianter: dict) -> str:
    """Moensteret der matchede, uafhaengigt af hvordan OCR skrev det."""
    lav = traf.casefold()
    for m in varianter:
        if m.casefold() == lav:
            return m
    return traf


def _rects_overlap(a, b) -> bool:
    for (ax0, ay0, ax1, ay1) in a:
        for (bx0, by0, bx1, by1) in b:
            if ax0 < bx1 and ax1 > bx0 and ay0 < by1 and ay1 > by0:
                return True
    return False


# ---------------------------------------------------------------------------
# Gruppering
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Group:
    """Alle forekomster af den samme vaerdi, paa tvaers af filer og sider."""

    entity: str
    key: str
    display: str
    findings: tuple
    role_hint: str = ""

    @property
    def count(self) -> int:
        return len(self.findings)


def group_findings(findings) -> list:
    """Saml fund pr. (entitet, normaliseret vaerdi).

    Det er grupperingen der giver kravet "fravaelg advokatens navn én gang":
    ét klik fjerner navnet i hele bundtet, uanset hvor mange sider det staar paa.
    """
    buckets = {}
    for f in findings:
        buckets.setdefault((f.entity, f.key), []).append(f)

    groups = []
    for (entity, key), items in buckets.items():
        # Vis den hyppigste originale stavemaade, ikke den normaliserede form.
        # Ved uafgjort vinder den KORTESTE: "Anne Hansen" er en bedre etiket end
        # "Advokat Anne Hansen", selv om begge forekommer én gang.
        spelling = {}
        for f in items:
            v = re.sub(r"\s+", " ", f.value).strip()
            spelling[v] = spelling.get(v, 0) + 1
        display = max(spelling.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]

        hints = [f.role_hint for f in items if f.role_hint]
        if not hints:
            hint = ""
        elif len(set(hints)) == 1:
            hint = hints[0]
        else:
            hint = ROLE_MIXED

        groups.append(Group(entity, key, display, tuple(items), hint))

    groups.sort(key=_group_sort_key)
    return groups


#: Raekkefoelge i dialogen: de sikre identifikatorer foerst, saa navne, saa stoej.
_ENTITY_ORDER = (pii.CPR, pii.CPR_LOOSE, pii.CVR, pii.EMAIL, pii.IBAN,
                 pii.CREDIT_CARD, pii.PHONE, pii.ACCOUNT, pii.PERSON,
                 pii.ADDRESS, pii.POSTAL, pii.CASE_NO, pii.ORGANIZATION,
                 pii.LOCATION)


def entity_rank(entity: str) -> int:
    try:
        return _ENTITY_ORDER.index(entity)
    except ValueError:
        return len(_ENTITY_ORDER)


def _group_sort_key(g: Group):
    # Navne med en professionel rolle oeverst: det er dem brugeren skal tage
    # stilling til, og de skal ikke ligge begravet i en lang navneliste.
    professional = 0 if (g.role_hint and
                         ROLE_PROFESSIONAL.fullmatch(g.role_hint)) else 1
    if g.role_hint == ROLE_MIXED:
        professional = 0
    return (entity_rank(g.entity), professional, -g.count, g.display.casefold())


def preselect(groups, decisions) -> set:
    """Fund-uid'er der skal vaere afkrydsede naar dialogen aabner.

    **Alt er valgt som standard** — undtagen grupper brugeren udtrykkeligt har
    fravalgt i en tidligere koersel. Ellers ville anden koersel maskere praecis
    dét foerste koersel bevidst lod staa.
    """
    out = set()
    for g in groups:
        if decisions.get(g.key, True):
            out.update(f.uid for f in g.findings)
    return out


def remembered_keys(groups, decisions) -> list:
    """Grupper der er forud-fravalgte, saa dialogen kan sige det hoejt."""
    return [g.display for g in groups if decisions.get(g.key, True) is False]


# ---------------------------------------------------------------------------
# Fund -> AnnotationSpec
# ---------------------------------------------------------------------------

def _covered(rects, existing) -> bool:
    """Er ``rects`` allerede daekket af en eksisterende maskering (±1 pt)?"""
    tol = 1.0
    for r in rects:
        hit = False
        for e in existing:
            if (abs(r[0] - e[0]) <= tol and abs(r[1] - e[1]) <= tol
                    and abs(r[2] - e[2]) <= tol and abs(r[3] - e[3]) <= tol):
                hit = True
                break
        if not hit:
            return False
    return True


def build_items(findings, selected_uids, model) -> list:
    """``[(page_uid, AnnotationSpec)]`` — én spec pr. valgt forekomst.

    Per-forekomst og ikke pr. side, fordi ``AnnotationSpec.label`` baerer
    *grunden* til maskeringen: en side-spec der blandede CPR og navne ville
    vaere noedt til at lyve om den. Det koster intet ved gem —
    ``annotations.apply_specs_to_page`` kalder ``apply_redactions()`` én gang
    uanset antal specs.
    """
    # Allerede maskerede omraader pr. side, saa en gentagen koersel ikke stabler
    # duplikater oven paa hinanden.
    existing = {}
    for _entry, page in model.flatten():
        rects = []
        for spec in page.annots:
            if spec.kind == an.ANNOT_REDACT and spec.source in ("presidio", "search"):
                rects.extend(spec.rects)
        if rects:
            existing[page.uid] = rects

    items = []
    for f in findings:
        if f.uid not in selected_uids:
            continue
        if _covered(f.rects, existing.get(f.page_uid, ())):
            continue
        items.append((f.page_uid, em.AnnotationSpec(
            kind=an.ANNOT_REDACT, rects=tuple(f.rects),
            color=(0, 0, 0), fill=(0, 0, 0),
            source="presidio", label=f.entity)))
    return items


def image_pages(model) -> set:
    """Side-uid'er der stammer fra en billedfil.

    Bruges af UI'en til at advare hvis der maskeres i billeder — geometrien
    bages ind i A4-rasteren ved gem (se ``save_pipeline``).
    """
    return {p.uid for _f, p in model.flatten()
            if em.kind_for_path(p.src_path) == em.KIND_IMAGE}
