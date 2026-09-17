"""Pseudonymisering af dokumenttekst til udklipsholderen.

Modulet er **GUI-frit** — hovedsuiten koerer det headless, praecis som
``anonymize``, ``pii`` og ``ocr_text``. Importer ikke Qt herfra.

Hvorfor et modul ved siden af :mod:`anonymize` og ikke en tilfoejelse til det
--------------------------------------------------------------------------
De to funktioner deler *fundene* men ikke deres produkt:

* ``anonymize`` skal bruge **geometri**. Hvert fund baerer rects i A-space, saa
  en sort bjaelke kan lande praecis oven paa ordet. Hele OCR-, rotations- og
  ``ViewTransform``-maskineriet findes for den ene grunds skyld.
* Her skal der bruges **tekst**. Der laegges ikke noget oven paa siden; der
  skrives en ny streng. Geometrien er derfor ikke bare unoedvendig — den er
  aktivt i vejen, fordi den tvinger tekstudtraekket ned i ordlisten fra
  ``ocr_text.words_to_text`` og dermed smider afsnit, overskrifter og tabeller
  vaek. En sprogmodel skal have layoutet med.

Derfor: **markdown naar siden har et tekstlag, OCR-ord naar den ikke har.**
``pymupdf4llm`` er allerede afhaengigheden bag .md/.epub-eksporten, saa der
kommer ingen ny med.

Kontrakten der binder de to sammen er :func:`pii.normalize_value` — den samme
gruppenoegle som ``anonymize.group_findings`` bruger. Det er dét der giver
kravet "samme person, samme pseudonym paa tvaers af alle sider og filer", og
det er ogsaa dét der goer at :class:`dialogs.AnonymizeReviewDialog` kan
genbruges uaendret: den roerer aldrig ``Finding.rects``.

Ingen vej tilbage
-----------------
Ombytningen er **envejs**. Der gemmes ingen noegle i den kopierede tekst, og
oversigten (:func:`overview_text`) er den eneste forbindelse mellem pseudonym
og original. Den skal derfor *ikke* uploades nogen steder — filen siger det
selv paa foerste linje.

Og: pseudonymisering fjerner ikke identificerbarhed. "Person 1, ansat siden
2003 i Organisation 1, sag BS-4711" kan stadig udpege én bestemt person.
Teksten i dialogen skal blive ved med at sige *reducerer*, ikke *fjerner*.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime

from . import anonymize
from . import edit_model as em
from . import langdetect
from . import ocr_text
from . import pii
from .localization import LocalizationManager
from .logging_config import get_logger

# ``pdf_renderer`` og ``export_formats`` traekker Qt henholdsvis pymupdf4llm ind.
# Begge importeres DOVENT inde i funktionerne, saa modulet kan importeres GUI-frit.

_ = LocalizationManager.get_text
logger = get_logger(__name__)

#: Skillelinje mellem sider i den samlede tekst. En sprogmodel laeser den som
#: en overskrift, og et menneske kan se hvilken side en oplysning kom fra.
PAGE_MARKER = "\n\n===== %s =====\n\n"

#: Foerste linje i oversigtsfilen. Ordret som aftalt — den er hele pointen med
#: filen og maa ikke fortyndes.
OVERVIEW_WARNING = _(
    "Oversigt over pseudonymier. Du må ikke uploade disse til AI.")


def entity_label(entity: str) -> str:
    """Menneskeligt navn for en entitetstype, brugt som stamme i pseudonymet."""
    return {
        pii.CPR: _("CPR-nummer"),
        pii.CPR_LOOSE: _("CPR-nummer"),
        pii.CVR: _("CVR-nummer"),
        pii.PHONE: _("Telefonnummer"),
        pii.ACCOUNT: _("Kontonummer"),
        pii.ADDRESS: _("Adresse"),
        pii.POSTAL: _("Postnr-by"),
        pii.CASE_NO: _("Sagsnummer"),
        pii.PERSON: _("Person"),
        pii.LOCATION: _("Sted"),
        pii.ORGANIZATION: _("Organisation"),
        pii.EMAIL: _("E-mail"),
        pii.IBAN: _("IBAN"),
        pii.CREDIT_CARD: _("Betalingskort"),
    }.get(entity, _("Oplysning"))


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PageText:
    """Én sides tekst, klar til analyse. Ren data — krydser traadgraensen."""

    page_uid: str
    label: str                 # "Alfa.pdf · side 3"
    text: str
    ocr: bool = False          # teksten kom fra tekstgenkendelse
    english: bool = False      # den engelske model koerte ogsaa


@dataclass(frozen=True)
class Pseudonym:
    """Én ombytning: hvad der stod, og hvad der kom til at staa."""

    key: str                   # pii.normalize_value(entity, value)
    entity: str
    label: str                 # "Advokat 1"
    display: str               # hyppigste originale stavemaade
    count: int


@dataclass
class ScanText:
    """Resultatet af :func:`scan`. Baerer alt dialogen og ombytningen skal bruge."""

    pages: tuple = ()          # PageText
    findings: tuple = ()       # anonymize.Finding (rects er tomme)
    #: ``fund-uid -> (side-uid, start, slut)``. Ombytningen sker paa disse
    #: offsets; ``Finding`` har dem ikke, og at parse dem ud af ``uid`` ville
    #: goere uid'ets format til en kontrakt det ikke er.
    spans: dict = field(default_factory=dict)
    pages_scanned: int = 0
    pages_ocred: int = 0
    pages_failed: int = 0
    pages_english: int = 0
    pages_english_missing: int = 0
    cancelled: bool = False


# ---------------------------------------------------------------------------
# Tekstudtraek
# ---------------------------------------------------------------------------

def build_jobs(model, only_uids=None) -> list:
    """Snapshot af siderne. **Kaldes paa UI-traaden.** Se ``anonymize.build_jobs``."""
    return anonymize.build_jobs(model, only_uids)


def _markdown_for_page(doc, src_index: int) -> str:
    """Markdown for én side i et aabent dokument, eller "" hvis det ikke lykkes.

    Rotationen nulstilles paa en midlertidig ét-sides kopi foer udtraekket:
    ``/Rotate`` er en visningsindstilling, men pymupdf4llm's layoutanalyse
    tolker den som indhold og maser en hel side ned i én tabelraekke. Samme
    grund som ``export_formats._cleared_work_doc`` — bare for én side, saa et
    500-siders bundt ikke kopieres for at kunne laese side 3.

    Efterbehandlingen deles med fileksporten (``export_formats._page_markdown``),
    saa en kopieret tabel faar de samme kolonneoverskrifter som en eksporteret.
    """
    import pymupdf
    import pymupdf4llm
    from . import export_formats

    page = doc[src_index]
    tmp = None
    try:
        if page.rotation:
            tmp = pymupdf.open()
            tmp.insert_pdf(doc, from_page=src_index, to_page=src_index)
            tmp[0].set_rotation(0)
            work, index = tmp, 0
        else:
            work, index = doc, src_index
        chunks = pymupdf4llm.to_markdown(work, pages=[index],
                                         **export_formats._MD_KWARGS)
        if not chunks:
            return ""
        return export_formats._page_markdown(work, index, chunks[0])
    finally:
        if tmp is not None:
            tmp.close()


def page_text(path, kind, src_index, rotation, passwords, *,
              allow_ocr=True, cancel=None) -> tuple:
    """``(tekst, brugte_ocr)`` for én side. Kalderen skal holde ``PDF_LOCK``.

    Tekstlag -> markdown (overskrifter og tabeller overlever). Intet tekstlag
    -> tekstgenkendelse via ``ocr_text``, som ogsaa retter en side der er
    scannet paa tvaers. Rasteriseret tekst har intet layout at bevare, saa
    ordlisten er lige saa god som det bliver.
    """
    from . import pdf_renderer

    if kind == em.KIND_IMAGE:
        if not allow_ocr:
            return "", False
        words = ocr_text.image_words_a_space(path, rotation_hint=rotation,
                                             cancel=cancel)
        text, _offsets = ocr_text.words_to_text(words)
        return text, bool(words)

    doc = pdf_renderer._doc_cache.get_or_open(path, passwords)
    if doc is None or src_index >= doc.page_count:
        return "", False
    page = doc[src_index]

    if not ocr_text.needs_ocr(page):
        try:
            md = _markdown_for_page(doc, src_index)
        except Exception as e:
            logger.info("Markdown-udtraek fejlede paa %s side %s: %s",
                        path, src_index, e)
            md = ""
        if md.strip():
            return md, False
        # Faldt markdown-vejen sammen, er der stadig et tekstlag at hente.
        words = ocr_text.page_words_a_space(page, allow_ocr=False,
                                            rotation_hint=rotation)
        text, _offsets = ocr_text.words_to_text(words)
        return text, False

    if not allow_ocr:
        return "", False
    words = ocr_text.page_words_a_space(
        page, rotation_hint=rotation, cancel=cancel,
        cache_path=path, cache_index=src_index)
    text, _offsets = ocr_text.words_to_text(words)
    return text, bool(words)


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def scan(jobs, passwords, *, allow_ocr=True, analyze=True, on_progress=None,
         cancel=None) -> ScanText:
    """Læs teksten og find personoplysningerne. **Arbejdstraad.**

    ``PDF_LOCK`` holdes pr. side, aldrig om en hel fil: OCR af én side tager
    sekunder, og laasen ville ellers fryse miniature-optegningen imens.
    Analysen bagefter er ren Python og koerer uden for laasen.

    ``analyze=False`` henter kun teksten. Det er ikke en mikro-optimering:
    ``pii._engine()`` koster ~5 sekunder og ~300 MB RAM foerste gang, og en
    bruger der bare vil kopiere teksten raa skal ikke betale for en sprogmodel
    hen ikke bruger.
    """
    from . import pdf_renderer

    pages, findings = [], []
    spans_by_uid = {}
    scanned = ocred = failed = english = english_missing = 0
    total = len(jobs)

    for i, (page_uid, path, kind, src_index, rotation, label) in enumerate(jobs):
        if cancel is not None and cancel.is_set():
            return ScanText(tuple(pages), tuple(findings), spans_by_uid,
                            scanned, ocred, failed, english, english_missing, True)
        try:
            with pdf_renderer.PDF_LOCK:
                text, used_ocr = page_text(path, kind, src_index, rotation,
                                           passwords, allow_ocr=allow_ocr,
                                           cancel=cancel)
        except Exception as e:
            logger.warning("Kunne ikke laese tekst fra %s side %s: %s",
                           path, src_index, e)
            text, used_ocr = "", False
            failed += 1

        scanned += 1
        if used_ocr:
            ocred += 1

        if text.strip() and not analyze:
            pages.append(PageText(page_uid, label, text, used_ocr, False))
        elif text.strip():
            is_en = langdetect.looks_english(text)
            if is_en:
                if pii.availability(pii.LANGUAGE_EN)[0]:
                    english += 1
                else:
                    english_missing += 1
            pages.append(PageText(page_uid, label, text, used_ocr, is_en))
            try:
                spans = pii.analyze(text, ocr=used_ocr, english=is_en)
            except pii.ModelMissing:
                raise
            except Exception as e:
                logger.warning("Analysen fejlede paa %s: %s", label, e)
                spans = []
            for s in spans:
                value = text[s.start:s.end]
                uid = "%s:%d:%d" % (page_uid, s.start, s.end)
                findings.append(anonymize.Finding(
                    page_uid=page_uid, entity=s.entity, value=value,
                    rects=(), context=anonymize._context_for(text, s.start, s.end),
                    page_label=label, score=s.score,
                    role_hint=anonymize.role_hint(text, s.start, s.end),
                    key=pii.normalize_value(s.entity, value), uid=uid))
                spans_by_uid[uid] = (page_uid, s.start, s.end)

        if on_progress is not None:
            on_progress(i + 1, total, label)

    if analyze:
        findings += _propagate(findings, pages, spans_by_uid)
    return ScanText(tuple(pages), tuple(findings), spans_by_uid,
                    scanned, ocred, failed, english, english_missing, False)


def _propagate(findings, pages, spans_by_uid) -> list:
    """Find de bekraeftede navne igen paa de sider hvor modellen overs dem.

    Samme begrundelse som ``anonymize._propagate_names``: OCR skriver
    ``RasmusNedergaard`` i ét ord paa nogle sider, modellen ser et ukendt ord og
    tagger det ikke — og navnet ville saa slippe umaskeret igennem netop dér.
    Her er det billigere end i ``anonymize``: teksten ligger allerede i
    ``pages``, saa der skal ikke laeses en side om.
    """
    varianter = anonymize.name_variants(findings)
    if not varianter:
        return []
    rx = re.compile(r"(" + "|".join(sorted(map(re.escape, varianter),
                                           key=len, reverse=True)) + r")",
                    re.IGNORECASE)
    per_side = {}
    for f in findings:
        per_side.setdefault(f.page_uid, []).append(f)

    ekstra = []
    for pt in pages:
        eksisterende = per_side.get(pt.page_uid, ())
        optaget = [spans_by_uid[f.uid] for f in eksisterende if f.uid in spans_by_uid]
        for m in rx.finditer(pt.text):
            if any(m.start() < slut and m.end() > start
                   for (_u, start, slut) in optaget):
                continue
            uid = "%s:%d:%d" % (pt.page_uid, m.start(), m.end())
            if uid in spans_by_uid:
                continue
            key = varianter[anonymize._variant_key(m.group(0), varianter)]
            ekstra.append(anonymize.Finding(
                page_uid=pt.page_uid, entity=pii.PERSON, value=m.group(0),
                rects=(), context=anonymize._context_for(pt.text, m.start(), m.end()),
                page_label=pt.label, score=0.99,
                role_hint=anonymize.role_hint(pt.text, m.start(), m.end()),
                key=key, uid=uid))
            spans_by_uid[uid] = (pt.page_uid, m.start(), m.end())
            optaget.append((pt.page_uid, m.start(), m.end()))
    if ekstra:
        logger.info("Navne-propagering tilfoejede %d fund", len(ekstra))
    return ekstra


# ---------------------------------------------------------------------------
# Tildeling af pseudonymer
# ---------------------------------------------------------------------------

def assign(findings, selected_uids=None) -> list:
    """Ét pseudonym pr. gruppe, nummereret i den raekkefoelge de foerst optraeder.

    Nummereringen foelger dokumentet og ikke gruppens stoerrelse: læser man
    teksten forfra, moeder man Person 1 foer Person 2, og saa er etiketten til
    at holde styr paa. Rollehintet bliver til stammen naar det findes
    (``Advokat 1``), fordi det er dét der goer den kopierede tekst laesbar for
    en sprogmodel — ellers taber man hvem der er hvem.
    """
    if selected_uids is not None:
        findings = [f for f in findings if f.uid in selected_uids]
    groups = anonymize.group_findings(findings)
    by_key = {g.key: g for g in groups}

    # Foerste forekomst i dokumentets raekkefoelge. ``findings`` kommer fra
    # ``scan`` i side-raekkefoelge, og propagerede fund er lagt bagest -- de er
    # pr. definition ekstra forekomster af et navn der allerede er set.
    order, seen = [], set()
    for f in findings:
        if f.key not in seen and f.key in by_key:
            seen.add(f.key)
            order.append(f.key)

    counters, out = {}, []
    for key in order:
        g = by_key[key]
        stem = _stem_for(g)
        counters[stem] = counters.get(stem, 0) + 1
        out.append(Pseudonym(key=g.key, entity=g.entity,
                             label="%s %d" % (stem, counters[stem]),
                             display=g.display, count=g.count))
    return out


def _stem_for(group) -> str:
    """Stammen i pseudonymet: rollen hvis den er entydig og professionel."""
    hint = group.role_hint
    if (group.entity == pii.PERSON and hint and hint != anonymize.ROLE_MIXED
            and anonymize.ROLE_PROFESSIONAL.fullmatch(hint)):
        return hint[:1].upper() + hint[1:]
    return entity_label(group.entity)


# ---------------------------------------------------------------------------
# Ombytning
# ---------------------------------------------------------------------------

def render(scan_result: ScanText, pseudonyms=None, selected_uids=None) -> str:
    """Den samlede tekst, med pseudonymer sat ind hvor der stod noget.

    ``pseudonyms=None`` giver den rene tekst uden ombytning. Ombytningen sker
    **bagfra** pr. side, saa et tidligere spens offsets ikke flytter sig.
    """
    labels = {p.key: p.label for p in (pseudonyms or ())}
    per_side = {}
    if labels:
        for f in scan_result.findings:
            if selected_uids is not None and f.uid not in selected_uids:
                continue
            if f.key not in labels:
                continue
            span = scan_result.spans.get(f.uid)
            if span is None:
                continue
            per_side.setdefault(span[0], []).append((span[1], span[2], labels[f.key]))

    ud = []
    for pt in scan_result.pages:
        text = pt.text
        edits = per_side.get(pt.page_uid)
        if edits:
            text = _splice(text, edits)
        ud.append(PAGE_MARKER % pt.label + text.strip())
    return "".join(ud).strip() + "\n"


def _splice(text: str, edits) -> str:
    """Byt ``(start, slut, tekst)`` ind i ``text``, bagfra og uden overlap."""
    ordnet = sorted(edits, key=lambda e: (e[0], -(e[1] - e[0])))
    beholdt, sidste = [], -1
    for start, slut, label in ordnet:
        if start < sidste:
            continue                       # overlapper et allerede valgt spen
        beholdt.append((start, slut, label))
        sidste = slut
    for start, slut, label in reversed(beholdt):
        text = text[:start] + label + text[slut:]
    return text


# ---------------------------------------------------------------------------
# Oversigten
# ---------------------------------------------------------------------------

def overview_rows(pseudonyms) -> list:
    """``[(pseudonym, original, type, antal), ...]`` -- sorteret som tildelt."""
    return [(p.label, p.display, entity_label(p.entity), p.count)
            for p in pseudonyms]


def overview_text(pseudonyms, *, fmt: str = "txt", sources=(),
                  when=None) -> str:
    """Oversigten som ren tekst eller markdown.

    ``fmt`` i {"txt", "md"}. PDF skrives af :func:`write_overview_pdf`, som
    bruger de samme raekker.
    """
    rows = overview_rows(pseudonyms)
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    linjer = []
    if fmt == "md":
        linjer.append("# " + OVERVIEW_WARNING)
        linjer.append("")
        linjer.append(_("Oprettet: %s") % stamp)
        if sources:
            linjer.append(_("Kilder: %s") % ", ".join(sources))
        linjer.append("")
        linjer.append("| %s | %s | %s | %s |" % (
            _("Pseudonym"), _("Oprindelig værdi"), _("Type"), _("Forekomster")))
        linjer.append("|---|---|---|---|")
        for label, display, typ, count in rows:
            linjer.append("| %s | %s | %s | %d |"
                          % (label, _md_cell(display), typ, count))
    else:
        linjer.append(OVERVIEW_WARNING)
        linjer.append("=" * len(OVERVIEW_WARNING))
        linjer.append("")
        linjer.append(_("Oprettet: %s") % stamp)
        if sources:
            linjer.append(_("Kilder: %s") % ", ".join(sources))
        linjer.append("")
        w = max([len(r[0]) for r in rows] + [len(_("Pseudonym"))])
        w2 = max([len(r[1]) for r in rows] + [len(_("Oprindelig værdi"))])
        head = "%-*s  %-*s  %s" % (w, _("Pseudonym"), w2,
                                   _("Oprindelig værdi"), _("Type"))
        linjer.append(head)
        linjer.append("-" * len(head))
        for label, display, typ, count in rows:
            linjer.append("%-*s  %-*s  %s (%d)" % (w, label, w2, display,
                                                   typ, count))
    if not rows:
        linjer.append(_("Der blev ikke fundet nogen personoplysninger."))
    return "\n".join(linjer) + "\n"


def _md_cell(value: str) -> str:
    """En roer i en vaerdi ville braekke markdown-tabellen."""
    return value.replace("|", "\\|").replace("\n", " ")


def write_overview_pdf(out_path: str, pseudonyms, *, sources=(), when=None) -> None:
    """Skriv oversigten som PDF med PyMuPDF. Ingen ny afhaengighed.

    Skrifterne er de indbyggede Base-14 (``helv``/``hebo``), saa der bundtes
    ingen fontfil — og de daekker latin-1, hvilket raekker til danske navne.
    """
    import pymupdf

    rows = overview_rows(pseudonyms)
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
    doc = pymupdf.open()
    margin, top, bottom, leading = 56, 64, 56, 14
    page = doc.new_page()
    y = top

    def new_page():
        nonlocal page, y
        page = doc.new_page()
        y = top

    def write(text, *, font="helv", size=10, color=(0, 0, 0), gap=0):
        nonlocal y
        width = page.rect.width - 2 * margin
        for line in _wrap(text, width, font, size):
            if y > page.rect.height - bottom:
                new_page()
            page.insert_text((margin, y), line, fontname=font, fontsize=size,
                             color=color)
            y += leading * (size / 10.0)
        y += gap

    write(OVERVIEW_WARNING, font="hebo", size=14, color=(0.7, 0, 0), gap=6)
    write(_("Oprettet: %s") % stamp, size=9, color=(0.35, 0.35, 0.35))
    if sources:
        write(_("Kilder: %s") % ", ".join(sources), size=9,
              color=(0.35, 0.35, 0.35))
    y += 10

    if not rows:
        write(_("Der blev ikke fundet nogen personoplysninger."))
    else:
        write("%s — %s — %s" % (_("Pseudonym"), _("Oprindelig værdi"), _("Type")),
              font="hebo", size=10, gap=4)
        for label, display, typ, count in rows:
            write("%s  —  %s  (%s, %d)" % (label, display, typ, count))
    doc.save(out_path, garbage=3, deflate=True)
    doc.close()


def _wrap(text: str, width: float, font: str, size: float) -> list:
    """Ombryd paa ordgraenser efter den faktiske tekstbredde."""
    import pymupdf
    f = pymupdf.Font(fontname=font)
    ord_liste = text.split()
    if not ord_liste:
        return [""]
    linjer, cur = [], ord_liste[0]
    for w in ord_liste[1:]:
        kandidat = cur + " " + w
        if f.text_length(kandidat, size) <= width:
            cur = kandidat
        else:
            linjer.append(cur)
            cur = w
    linjer.append(cur)
    return linjer


def source_names(model, only_uids=None) -> list:
    """Filnavnene bag en koersel — til oversigtens hoved."""
    navne = []
    for entry in model.files:
        if only_uids is not None and not any(p.uid in only_uids
                                             for p in entry.pages):
            continue
        navn = os.path.basename(entry.path)
        if navn not in navne:
            navne.append(navn)
    return navne
