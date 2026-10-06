"""Genkendelse af personoplysninger (PII) i dansk tekst — Presidio-laget.

Modulet er **GUI-frit** og maa ikke importere Qt. Det er ogsaa bevidst
**import-let**: hverken ``spacy`` eller ``presidio_analyzer`` roeres paa
modulniveau. De koster tilsammen 2-5 sekunder og ~300 MB RAM, og appen skal
starte lige saa hurtigt som foer for de brugere der aldrig trykker paa
anonymiseringsknappen. Alt tungt sker foerst i :func:`_engine`.

Modellen loades **ved sti** (``spacy.load(str(model_dir()))``) og den faerdige
pipeline injiceres direkte i ``SpacyNlpEngine``. Det er ikke kosmetik: den
saedvanlige vej (``NlpEngineProvider`` -> ``spacy.util.get_lang_class``) slaar op
i ``catalogue``'s entry-point-register, og det er praecis dét der doer i en
Nuitka-standalone-build. Brug **ikke** ``NlpEngineProvider`` her.

Validatorerne og regexerne ligger paa topniveau, saa de kan testes uden at
hverken spaCy eller Presidio er installeret.
"""

import datetime
import functools
import importlib.util
import re
import unicodedata

from . import langdetect
from . import postnumre
from .localization import LocalizationManager
from .logging_config import get_logger
from .utils import resource_path

_ = LocalizationManager.get_text
logger = get_logger(__name__)

#: Dansk er **standardsproget**, ikke ét sprog blandt to. Den danske model
#: koeres paa hver eneste side; engelsk laegges oven i, naar
#: :func:`langdetect.looks_english` finder bevis for det. Baggrunden staar i
#: ``docs/anonymization.md``: de to fejlmuligheder er maalt og er ikke lige
#: slemme — den engelske model paa dansk tekst slugte en hel loenseddeltabel ind
#: i ét ``PERSON``-spen (overmaskering, synlig, kan fravaelges), mens den danske
#: model paa engelsk tekst kun fandt 12 af 18 navne (undermaskering, usynlig).
LANGUAGE = "da"
LANGUAGE_EN = "en"
LANGUAGES = (LANGUAGE, LANGUAGE_EN)

MODEL_NAMES = {LANGUAGE: "da_core_news_md", LANGUAGE_EN: "en_core_web_md"}
#: Bevaret navn for den danske model — compileren og ``fetch_model.py`` bruger det.
MODEL_NAME = MODEL_NAMES[LANGUAGE]

#: Under denne konfidens naar et fund ikke frem til brugeren. Saenkes den, drukner
#: dialogen i talstoej; haeves den, forsvinder de svage men vigtige CPR-varianter.
SCORE_THRESHOLD = 0.4

# Entitetsnoegler. Stabile logiknoegler — de persisteres i
# ``AnnotationSpec.label`` og maa **aldrig** oversaettes.
CPR = "DK_CPR"
CPR_LOOSE = "DK_CPR_MULIG"
PHONE = "DK_PHONE"
CVR = "DK_CVR"
ACCOUNT = "DK_ACCOUNT"
ADDRESS = "DK_ADDRESS"
POSTAL = "DK_POSTAL_CITY"
CASE_NO = "DK_CASE_NO"
PERSON = "PERSON"
LOCATION = "LOCATION"
ORGANIZATION = "ORGANIZATION"
EMAIL = "EMAIL_ADDRESS"
IBAN = "IBAN_CODE"
CREDIT_CARD = "CREDIT_CARD"

#: Raekkefoelge ved overlap: vinder staar foerst. Et 8-cifret loeb inde i et
#: CPR-nummer maa ikke ende som et telefonnummer.
_PRIORITY = (CPR, CPR_LOOSE, CVR, IBAN, CREDIT_CARD, ACCOUNT, PHONE, CASE_NO,
             EMAIL, ADDRESS, POSTAL, PERSON, ORGANIZATION, LOCATION)


class ModelMissing(Exception):
    """Sprogmodellen eller Presidio er ikke tilgaengelig i denne installation."""


# ---------------------------------------------------------------------------
# CPR
# ---------------------------------------------------------------------------

_CPR_WEIGHTS = (4, 3, 2, 7, 6, 5, 4, 3, 2, 1)
_CVR_WEIGHTS = (2, 7, 6, 5, 4, 3, 2, 1)


def cpr_century(day7: int, yy: int) -> int:
    """Aarhundredet et CPR-nummer hoerer til, efter CPR-kontorets tabel."""
    if day7 <= 3:
        return 1900
    if day7 == 4 or day7 == 9:
        return 2000 if yy <= 36 else 1900
    return 2000 if yy <= 57 else 1800


def valid_cpr_date(digits: str) -> bool:
    """Er de foerste seks cifre en gyldig foedselsdato?

    Det er den eneste **haarde** kontrol vi har paa et CPR-nummer, og den fanger
    langt de fleste falske positiver (belob, datoer, sagsnumre).
    """
    if len(digits) != 10 or not digits.isdigit():
        return False
    dd, mm, yy = int(digits[0:2]), int(digits[2:4]), int(digits[4:6])
    # Erstatningspersonnummer: dag + 60 naar den rigtige dag er optaget.
    if dd > 31:
        dd -= 60
    try:
        datetime.date(cpr_century(int(digits[6]), yy) + yy, mm, dd)
    except ValueError:
        return False
    return True


def cpr_mod11_ok(digits: str) -> bool:
    """Den klassiske modulus 11-kontrol.

    **Afskaffet 1. oktober 2007.** Numre udstedt efter den dato fejler den
    lovligt, saa den maa kun bruges til at *haeve* konfidensen — aldrig til at
    afvise. Se :meth:`_CprRecognizer.validate_result`.
    """
    if len(digits) != 10 or not digits.isdigit():
        return False
    return sum(w * int(c) for w, c in zip(_CPR_WEIGHTS, digits)) % 11 == 0


def cvr_mod11_ok(digits: str) -> bool:
    """Modulus 11 for CVR-numre. Her er kontrollen **stadig** gaeldende."""
    if len(digits) != 8 or not digits.isdigit():
        return False
    return sum(w * int(c) for w, c in zip(_CVR_WEIGHTS, digits)) % 11 == 0


# ---------------------------------------------------------------------------
# Postnummer + by
# ---------------------------------------------------------------------------

def _fold_city(name: str) -> str:
    """Bynavn paa sammenlignelig form: smaat, uden mellemrum, tegn og accenter.

    ``æ/ø/å`` foldes til ``ae/oe/aa``, fordi OCR jaevnligt laeser dem forkert og
    aeldre dokumenter translittererer dem.
    """
    s = unicodedata.normalize("NFKC", name or "").casefold()
    s = s.replace("æ", "ae").replace("ø", "oe").replace("å", "aa")
    return "".join(ch for ch in s if ch.isalnum())


def _edits_within(a: str, b: str, limit: int) -> bool:
    """Er redigeringsafstanden mellem ``a`` og ``b`` hoejst ``limit``?

    En fuld Levenshtein paa to bynavne er billig nok, og baandet gemmer os for
    at bygge hele matricen naar laengderne allerede er for langt fra hinanden.
    """
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, start=1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1,
                         prev[j - 1] + (ca != cb))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def _ocr_slack(name: str) -> int:
    """Hvor mange tegns afvigelse et bynavn maa have.

    Maalt paa et OCR'et processkrift: ``Malling`` blev laest som ``Mallin``,
    ``Mailin``, ``Malli`` og ``Maltin`` — op til **to** tegns afvigelse paa et
    navn med syv bogstaver — og ``Aarhus V`` som ``Arhus V``. Korte navne faar
    mindre slack, ellers ville ``Ry`` og ``Rø`` blive samme by.
    """
    n = len(name)
    if n <= 4:
        return 0
    return 1 if n <= 6 else 2


@functools.lru_cache(maxsize=1)
def _cities():
    """Bynavne paa foldet form -> postnumre.

    Indekset bygges **her** og ikke i den genererede fil, saa der kun findes én
    normalisering. Da den genererede fil byggede sit eget, kom de to ud af trit:
    ``"København V"`` foldet med ``casefold()`` blev ``københavnv``, mens
    validatoren folder ø til oe og ledte efter ``koebenhavnv``.
    """
    out = {}
    for nr, navn in postnumre.POSTNUMRE.items():
        out.setdefault(_fold_city(navn), set()).add(nr)
    return out


def postal_match(text: str):
    """Presidios tre-vaerdi-svar for et ``postnummer + by``-match.

    ``True`` naar tallet er et rigtigt postnummer **og** navnet er byens (evt.
    med OCR-slitage), eller naar navnet alene er et kendt dansk bynavn.
    ``False`` ellers.

    Uden den kontrol matchede moensteret loenartsnumre og aarstal fra
    loensedler: ``1503 Lørdag``, ``1511 Overarbejde``, ``2025 Tiludbetaling``,
    ``2026 Firmapension``. Maalt paa ét rigtigt processkrift gav det **46**
    falske "byer" — flere end der var rigtige fund i hele dokumentet.
    """
    m = re.match(r"\s*(\d{4})[ \t]+(.+?)\s*$", text or "", re.DOTALL)
    if not m:
        return False
    code, city = int(m.group(1)), m.group(2)
    folded = _fold_city(city)
    if not folded:
        return False

    official = postnumre.POSTNUMRE.get(code)
    if official is not None:
        want = _fold_city(official)
        if folded == want or _edits_within(folded, want, _ocr_slack(want)):
            return True

    # Tallet kan vaere fejllaest, eller vaere et postboksnummer der ikke staar i
    # den officielle liste (1504 er fx ikke med, men "København V" er en by).
    # Er navnet i sig selv et kendt bynavn, er det stadig en adresseoplysning.
    if folded in _cities():
        return True
    return False


def postal_prefix(text: str):
    """Det laengste ``postnummer + by`` i starten af ``text`` der kan valideres.

    ``POSTAL_RE`` tager op til to ord efter bynavnet med, fordi bynavne kan
    vaere flerleddede (``Kongens Lyngby``, ``Hvide Sande``). Moensteret er
    graadigt, saa staar der et ord med stort efter byen — ``8500 Grenaa Hun
    sagde`` eller en etiket i naeste spalte — kommer det med, valideringen
    afviser helheden, og Presidio proever aldrig det kortere match. Saa
    forsvandt postnummeret helt: **under**maskering. Her skrelles ord af bagfra
    til et praefiks valideres. ``None`` hvis intet goer.
    """
    s = (text or "").rstrip()
    while True:
        cut = max(s.rfind(" "), s.rfind("\t"), s.rfind("-"))
        if cut <= 0:
            return None
        s = s[:cut].rstrip()
        if not re.search(r"[^\W\d_]", s):     # kun postnummeret tilbage
            return None
        if postal_match(s):
            return s


# ---------------------------------------------------------------------------
# Moenstre
# ---------------------------------------------------------------------------

_DAY = r"(?:0[1-9]|[12]\d|3[01]|6[1-9]|[78]\d|9[01])"   # inkl. erstatningsdag (+60)
_MON = r"(?:0[1-9]|1[0-2])"

CPR_STRICT = r"\b" + _DAY + _MON + r"\d{2}-\d{4}\b"
CPR_SPACED = r"\b" + _DAY + _MON + r"\d{2}\s\d{4}\b"
CPR_BARE = r"\b" + _DAY + _MON + r"\d{6}\b"
#: Loes CPR-form til OCR'ede sider: ét fejllaest ciffer maa ikke goere et
#: CPR-nummer usynligt, saa her accepteres vilkaarlige separatorer.
CPR_LOOSE_RE = re.compile(r"\b\d[\d .\-]{8,14}\d\b")

# Ingen ``\b`` foran ``\+``: mellem et mellemrum og et plus ER der ingen
# ordgraense, saa moensteret ville aldrig matche. ``(?<![\d+])`` goer arbejdet.
PHONE_INTL = r"(?<![\d+])(?:\+45|0045)[ .\-]?[2-9]\d(?:[ .\-]?\d{2}){3}\b"
PHONE_GROUPED = r"\b[2-9]\d(?:[ .]\d{2}){3}\b"
PHONE_BARE = r"\b[2-9]\d{7}\b"

CVR_RE = r"\b(?:DK[ ]?)?\d{8}\b"
ACCOUNT_RE = r"\b\d{4}[ .\-]\d{6,10}\b"

_STREET_SUFFIX = (r"vej|vejen|gade|gaden|allé|alle|allee|boulevard|blvd|plads|"
                  r"stræde|straede|torv|torvet|park|parken|have|haven|bakke|bakken|"
                  r"toften|engen|lunden|dalen|marken|vangen|vænget|vaenget|sti|"
                  r"stien|kaj|kajen|brygge|bryggen|gårdsvej|gaardsvej|krogen|"
                  r"højen|hoejen|ager|agre")
# To ting er bevidste her:
#
# ``(?-i:...)`` — Presidio kompilerer moenstre med ``re.IGNORECASE``, saa uden den
# scopede flag matcher "... 2024 dom i sagen ..." som postnummer + by.
#
# ``[ \t]`` og ikke ``\s`` — ``\s`` daekker linjeskift, og saa matchede
# "…marts 2024\nAdvokat Anne Hansen" som postnummer + by. Det er ikke bare stoej:
# det laengste spen vinder overlapskonkurrencen, saa den falske adresse slugte
# advokatens navn og gjorde det umuligt at fravaelge ham alene.
#: Samme endelser med stort begyndelsesbogstav. De er noedvendige, fordi et
#: dansk vejnavn lige saa ofte er FLERE ord ("Marius Holsts Gade", "Gammel
#: Kongevej") som ét, og sidste ord er dér skrevet med stort. Maalt: uden dem
#: fandt ``DK_ADDRESS`` intet paa en loenseddel med "Marius Holsts Gade 8,
#: 3. tv.", og navnegenkenderen tog den i stedet — som en PERSON.
_STREET_SUFFIX_CAP = "|".join(s[:1].upper() + s[1:]
                              for s in _STREET_SUFFIX.split("|"))
#: Selve vejordet: enten en sammensaetning ("Vestergade") eller endelsen alene
#: med stort ("Gade"). ``{0,2}?`` foran er **doven**, saa moensteret hellere tager
#: faerre ord med: ellers kunne "Advokat Anne Hansen Vestergade 12" sluge
#: advokatens navn ind i adressen, og det laengste spen vinder overlapskampen.
_STREET_WORD = (r"(?:[A-ZÆØÅ][\wÆØÅæøå.'\-]{0,25}(?:" + _STREET_SUFFIX + r")"
                r"|(?:" + _STREET_SUFFIX_CAP + r"))")
#: Husnummerbogstavet skal staa alene (``25 B``, ``25B``): uden lookahead'en tog
#: moensteret ``N`` fra ``Normtid`` i ``Thorsvej 25 Normtid``, og rectet fulgte
#: hele ordet med.
ADDRESS_RE = (r"(?-i:\b(?:[A-ZÆØÅ][\wÆØÅæøå.'\-]{0,20}[ \t]+){0,2}?"
              + _STREET_WORD + r")"
              r"[ \t]+\d{1,4}(?:[ \t]*[A-Za-z](?![\wÆØÅæøå]))?"
              r"(?:[ \t]*,?[ \t]*\d{1,2}\.?[ \t]*(?:sal)?[ \t]*,?[ \t]*"
              r"(?:t\.?[hv]\.?|m\.?f\.?))?")
POSTAL_RE = (r"\b(?:1[0-9]{3}|[2-9][0-9]{3})[ \t]+"
             r"(?-i:[A-ZÆØÅ][a-zæøå]{2,}(?:[ \-][A-ZÆØÅ]?[a-zæøå]+){0,2}"
             r"(?:[ \t][A-ZÆØÅ]\b)?)")

CASE_COURT_RE = re.compile(
    r"\b(?:BS|SKS|FS|A\.?S\.?|B\.?S\.?|V\.?L\.?|Ø\.?L\.?)[ \-]?\d{1,6}[/\-]\d{4}"
    r"(?:-[A-ZÆØÅ]{2,5})?\b")
CASE_KEYED_RE = re.compile(
    r"(?:j\.?\s?nr\.?|journalnr\.?|journalnummer|sagsnr\.?|sagsnummer|"
    r"akt\.?\s?nr\.?)\s*[:.]?\s*([\w/\-][\w./\-]{2,28}[\w/\-])", re.IGNORECASE)

#: ``da_core_news_md`` taggger jaevnligt retsinstanser og myndigheder som PER.
#: De er ikke personoplysninger, og de fylder dialogen med stoej.
_NAME_STOPWORDS = frozenset("""
retten byretten landsretten landsret højesteret hoejesteret handelsretten
statsforvaltningen familieretshuset politiet anklagemyndigheden ankestyrelsen
skattestyrelsen udlændingestyrelsen kommune kommunen regionen staten
domstolsstyrelsen advokatnævnet retslægerådet arbejdsretten
januar februar marts april maj juni juli august september oktober november
december mandag tirsdag onsdag torsdag fredag lørdag søndag
cpr cvr tlf telefon mobil mail e-mail journalnr sagsnr adresse postnr att
bilag side dato reg.nr kontonr
hr fru frk hr. fru. frk.
mr mrs ms miss dr sir madam mr. mrs. ms. dr.
""".split())

_INSTITUTION_RE = re.compile(
    r"^(?:Retten|Byretten|Landsretten|Østre|Vestre|Højesteret|Sø-\s*og\s*Handelsretten|"
    r"Statsforvaltningen|Familieretshuset|Politiet|Anklagemyndigheden)\b")


# ---------------------------------------------------------------------------
# Model- og motoropslag
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=len(LANGUAGES))
def model_dir(lang: str = LANGUAGE):
    """Mappen med sprogets spaCy-model, eller None hvis den ikke er installeret.

    Modellerne ligger ved siden af exe'en som ``models/<modelnavn>`` — samme
    moenster som ``tessdata``. Pip-layoutet har et ekstra versioneret niveau
    (``da_core_news_md/da_core_news_md-3.8.0/``), saa begge former accepteres.
    """
    name = MODEL_NAMES.get(lang)
    if name is None:
        return None
    try:
        base = resource_path("models") / name
        if (base / "config.cfg").exists():
            return base
        for child in sorted(base.iterdir()):
            if child.is_dir() and (child / "config.cfg").exists():
                return child
    except OSError:
        pass
    except Exception as e:
        logger.info("Kunne ikke slaa sprogmodellen '%s' op: %s", name, e)
    return None


def availability(lang: str = LANGUAGE):
    """``(tilgaengelig, forklaring)`` — sikker at kalde ved opstart.

    Roerer kun filsystemet og importtabellen; **importerer aldrig spaCy**, saa
    kommandobaren kan bruge den til at afgoere om knappen skal vaere aktiv.

    Uden argument svarer den paa **dansk**, som er dét knappen afhaenger af.
    Mangler kun den engelske model, virker anonymiseringen fortsat; engelske
    sider faar saa faerre navne fundet, og det siges i gennemgangsdialogen.
    """
    if model_dir(lang) is None:
        if lang == LANGUAGE_EN:
            return False, _("Den engelske sprogmodel er ikke installeret.")
        return False, _("Den danske sprogmodel er ikke installeret. Kør "
                        "installationsprogrammet igen og vælg "
                        "\"Dansk sprogmodel til automatisk anonymisering\".")
    if importlib.util.find_spec("presidio_analyzer") is None:
        return False, _("Anonymiseringsmotoren mangler i denne installation.")
    return True, ""


def _build_recognizers(lang: str = LANGUAGE):
    """Den vettede liste af genkendere for ét sprog.

    ``load_predefined_recognizers()`` kaldes bevidst **ikke**: den slaeber
    snesevis af lande-specifikke genkendere med (US SSN, UK NINO, spansk NIF),
    som paa danske tal er rene falsk-positiv-generatorer — og en
    Presidio-opgradering ville lydloest kunne tilfoeje flere.

    **Den engelske motor bidrager kun med navnegenkendelse.** Presidios egen
    dokumentation slaar fast at "a single recognizer serves one language only",
    saa CPR, CVR, konto og adresse skulle ellers instantieres én gang pr. sprog.
    Det er unoedvendigt: de haenger paa **jurisdiktionen**, ikke paa prosaen, og
    den danske motor koerer alligevel paa hver eneste side — ogsaa de engelske.
    Et CPR-nummer i et engelsk bilag findes altsaa af den danske motor, og de
    tunge regexer koeres kun én gang.
    """
    from presidio_analyzer.predefined_recognizers import SpacyRecognizer

    if lang != LANGUAGE:
        return [SpacyRecognizer(
            supported_language=lang,
            supported_entities=[PERSON, LOCATION, ORGANIZATION])]
    return _build_danish_recognizers()


def _build_danish_recognizers():
    """Hele det danske saet: formatgenkendere plus navnegenkendelse."""
    from presidio_analyzer import PatternRecognizer, Pattern
    from presidio_analyzer.predefined_recognizers import (
        EmailRecognizer, IbanRecognizer, CreditCardRecognizer, SpacyRecognizer)

    c = _classes()
    out = [
        c["Cpr"](),
        c["CprLoose"](),
        c["Phone"](),
        c["Cvr"](),
        c["CaseNo"](),
        PatternRecognizer(
            supported_entity=ACCOUNT, supported_language=LANGUAGE,
            name="DkAccountRecognizer",
            patterns=[Pattern("reg+konto", ACCOUNT_RE, 0.15)],
            # Uden kontekst ligger scoren UNDER taersklen med vilje: et bart
            # talpar er lige saa ofte en dato, et beloeb eller en sidehenvisning.
            context=["konto", "kontonr", "kontonummer", "reg.nr", "regnr",
                     "registreringsnummer", "bank", "indbetales", "overføres"]),
        PatternRecognizer(
            supported_entity=ADDRESS, supported_language=LANGUAGE,
            name="DkAddressRecognizer",
            patterns=[Pattern("vej+nr", ADDRESS_RE, 0.5)],
            context=["adresse", "bopæl", "boede", "bor", "beliggende", "c/o"]),
        c["Postal"](),
        EmailRecognizer(supported_language=LANGUAGE),
        IbanRecognizer(supported_language=LANGUAGE),
        CreditCardRecognizer(supported_language=LANGUAGE),
        # Modellen udsender DaNE-labels (PER/LOC/ORG/MISC). Selve oversaettelsen
        # til Presidios entiteter sker i NerModelConfiguration paa NlpEngine'en
        # (se _engine); ``check_label_groups`` er udgaaet og bliver IGNORERET.
        # Her begraenses kun HVILKE entiteter genkenderen melder tilbage.
        SpacyRecognizer(supported_language=LANGUAGE,
                        supported_entities=[PERSON, LOCATION, ORGANIZATION]),
    ]
    return out


#: Modellernes labels -> Presidios entiteter. Den danske model udsender
#: DaNE-labels (``PER``/``LOC``/``ORG``), den engelske OntoNotes-labels
#: (``PERSON``/``GPE``/``ORG``/``FAC``). ``FAC`` er med, fordi den daekker
#: navngivne veje og bygninger — "Baker Street" er en adresseoplysning.
NER_ENTITY_MAPPING = {
    "PER": PERSON,
    "PERSON": PERSON,
    "LOC": LOCATION,
    "GPE": LOCATION,
    "FAC": LOCATION,
    "ORG": ORGANIZATION,
}

#: Labels vi bevidst kasserer. ``MISC`` (dansk) og ``NORP`` (engelsk) daekker
#: nationaliteter, religioner og politiske grupper og er falsk-positiv-fabrikker
#: i retstekster. Resten er OntoNotes' tal- og datotyper, som vores egne
#: moenstergenkendere haandterer langt praeciseret. Uden listen logger Presidio
#: en advarsel pr. ukendt label pr. side.
NER_LABELS_IGNORED = {
    "MISC", "NORP", "DATE", "TIME", "MONEY", "PERCENT", "QUANTITY",
    "CARDINAL", "ORDINAL", "EVENT", "WORK_OF_ART", "LAW", "LANGUAGE",
    "PRODUCT",
}


@functools.lru_cache(maxsize=len(LANGUAGES))
def _engine(lang: str = LANGUAGE):
    ok, why = availability(lang)
    if not ok:
        raise ModelMissing(why)

    import spacy
    from presidio_analyzer import AnalyzerEngine, RecognizerRegistry
    from presidio_analyzer.nlp_engine import NerModelConfiguration, SpacyNlpEngine

    # ``exclude=["parser"]`` og ikke mere. Lemmatizer og attribute_ruler SKAL
    # blive: Presidios kontekstforstaerker matcher kontekstord mod *lemmaer*, og
    # uden dem doer hvert eneste ``context=[...]``-boost lydloest — hvilket er
    # dét der overhovedet faar DK_ACCOUNT og det bare 10-cifrede CPR til at virke.
    nlp = spacy.load(str(model_dir(lang)), exclude=["parser"])

    class _Loaded(SpacyNlpEngine):
        """NlpEngine over en pipeline vi allerede har i haanden.

        Omgaar ``NlpEngineProvider`` og dermed ``catalogue``'s
        entry-point-opslag, som ikke overlever Nuitka-standalone. Presidios egen
        dokumentation anbefaler netop ``NlpEngineProvider(conf_file=...)``, og
        dét er praecis den vej der ikke virker i den frosne build.
        """

        def __init__(self, loaded, ner_config, language):
            super().__init__(ner_model_configuration=ner_config)
            self.nlp = {language: loaded}

    # Mappingen SKAL ligge her og ikke paa genkenderen: ``check_label_groups`` er
    # udgaaet og bliver ignoreret, saa en mapping dér ville lydloest ikke virke.
    ner_config = NerModelConfiguration(
        model_to_presidio_entity_mapping=NER_ENTITY_MAPPING,
        labels_to_ignore=set(NER_LABELS_IGNORED))

    registry = RecognizerRegistry(supported_languages=[lang])
    for rec in _build_recognizers(lang):
        registry.add_recognizer(rec)

    return AnalyzerEngine(nlp_engine=_Loaded(nlp, ner_config, lang),
                          registry=registry, supported_languages=[lang],
                          default_score_threshold=SCORE_THRESHOLD)


def warmup(lang: str = LANGUAGE) -> None:
    """Byg motoren. Kald den fra en **arbejdstraad** — den tager 2-5 sekunder.

    Kun dansk varmes op ved scanningens start. Den engelske model loades foerst
    naar en side faktisk ser engelsk ud, saa et rent dansk dokument aldrig
    betaler de ~270 MB.
    """
    _engine(lang)


def reset() -> None:
    """Slip motorerne igen (bruges af tests og ved lav hukommelse)."""
    _engine.cache_clear()
    model_dir.cache_clear()
    for cache in _LOWER_PROPN.values():
        cache.clear()


# ---------------------------------------------------------------------------
# Analyse
# ---------------------------------------------------------------------------

class Span:
    """Et fund i en tekst. Ren data — ingen Presidio-typer slipper ud herfra."""

    __slots__ = ("entity", "start", "end", "score")

    def __init__(self, entity, start, end, score):
        self.entity = entity
        self.start = int(start)
        self.end = int(end)
        self.score = float(score)

    def __repr__(self):
        return "Span(%s, %d, %d, %.2f)" % (self.entity, self.start, self.end,
                                           self.score)


def analyze(text: str, *, ocr: bool = False, english=None) -> list:
    """Find personoplysninger i ``text``.

    ``ocr=True`` slaar den loese CPR-detektor til; den giver stoej paa ren tekst,
    men er noedvendig naar ét fejllaest ciffer ellers skjuler et CPR-nummer.

    ``english`` afgoer om den **engelske** model koeres oven i den danske.
    ``None`` (standard) lader :func:`langdetect.looks_english` afgoere det.
    Dansk koeres altid — ogsaa paa en side der ser engelsk ud — fordi CPR, CVR,
    konto og adresse haenger paa jurisdiktionen og ikke paa prosaen.
    """
    if not text or not text.strip():
        return []
    spans = _analyze_one(text, LANGUAGE, ocr=ocr)

    if english is None:
        english = langdetect.looks_english(text)
    if english and availability(LANGUAGE_EN)[0]:
        try:
            spans += _analyze_one(text, LANGUAGE_EN, ocr=ocr, ner_only=True)
        except Exception as e:                       # modellen kan vaere raadden
            logger.warning("Den engelske model kunne ikke koere: %s", e)

    return _prune_overlaps(_merge_duplicates(spans))


def _analyze_one(text: str, lang: str, *, ocr: bool, ner_only: bool = False):
    """Ét sprogs bidrag: koer motoren og luge stoejen ud med sprogets egne signaler."""
    engine = _engine(lang)
    # Koer sprogmodellen SELV og giv resultatet videre, i stedet for at lade
    # AnalyzerEngine goere det internt. Det koster ikke en ekstra koersel
    # (Presidio bruger netop de artefakter man raekker den), og til gengaeld
    # beholder vi ``artifacts.tokens`` — spaCy-dokumentet med ordklasser, som
    # ``_drop_name_noise`` bruger til at skille navne fra fagord.
    artifacts = engine.nlp_engine.process_text(text, lang)
    raw = engine.analyze(text=text, language=lang,
                         score_threshold=SCORE_THRESHOLD,
                         nlp_artifacts=artifacts)
    spans = [Span(r.entity_type, r.start, r.end, r.score) for r in raw]
    if ner_only:
        spans = [s for s in spans
                 if s.entity in (PERSON, ORGANIZATION, LOCATION)]
    if not ocr:
        spans = [s for s in spans if s.entity != CPR_LOOSE]
    return _drop_name_noise(spans, text, artifacts.tokens, lang)


def _merge_duplicates(spans: list) -> list:
    """Samme entitet fundet af begge modeller: behold det **tætteste** spen.

    ``_prune_overlaps`` beholder det *laengste*, og det er rigtigt naar to
    forskellige entiteter slaas om det samme omraade (et 8-cifret loeb inde i et
    CPR-nummer). Her er det forkert: den danske model kalder det
    ``and Mr. Simon Faarkrog Christensen``, den engelske ``Simon Faarkrog
    Christensen``, og det laengste ville give en gruppe i dialogen med
    konjunktion og titel klistret paa. Sammenlaegningen sker derfor **foer**
    beskaeringen og kun mellem fund af samme type.
    """
    out = []
    for s in sorted(spans, key=lambda s: (s.entity, s.start, s.end - s.start)):
        prev = out[-1] if out else None
        if (prev is not None and prev.entity == s.entity
                and s.start < prev.end and s.end > prev.start):
            # Overlapper og er samme type: behold den korteste, hoejeste score.
            if (s.end - s.start) < (prev.end - prev.start):
                out[-1] = s
            elif s.score > prev.score:
                out[-1] = s
            continue
        out.append(s)
    return out


_COURT_SEAT_RE = re.compile(
    r"(?:Retten|Byretten|Landsretten|Sø-\s*og\s*Handelsretten|"
    r"Familieretshuset|Politiet)\s+i\s*$")


def _name_token_index(doc):
    """Tokens der overhovedet kan indgaa i et navn, som ``(start, slut, propn,
    versal_bevaret)``.

    ``versal_bevaret`` er sandt naar tokenet er skrevet med stort OG
    lemmatiseringen beholder det store bogstav. Et faellesnavn der blot staar
    foerst i en celle bliver smaaskrevet af lemmatiseringen
    (``Helligdagstillæg`` -> ``helligdagstillæg``), mens et egennavn beholder
    sin versal (``Christensen`` -> ``Christensen``).
    """
    if doc is None:
        return ()
    out = []
    for t in doc:
        if not t.text[:1].isupper():
            continue
        out.append((t.idx, t.idx + len(t.text), t.pos_ == "PROPN",
                    t.lemma_[:1].isupper(), t.text))
    return tuple(out)


def _name_evidence(index, start: int, end: int):
    """``(har_propn, har_bevaret_versal, ord)`` for tokens der roerer spennet."""
    propn = keeps = False
    words = []
    for (a, b, is_propn, kept, word) in index:
        if a < end and b > start:
            propn = propn or is_propn
            keeps = keeps or kept
            words.append(word)
    return propn, keeps, tuple(words)


#: Neutrale saetninger som et ord proeves i med LILLE begyndelsesbogstav.
#: Den bare form er med, fordi et navn i en tabelcelle ogsaa staar alene.
#: Rammerne skal vaere paa **modellens eget sprog** — en engelsk model der faar
#: en dansk saetning svarer paa noget andet end det vi spoerger om.
_NAME_FRAMES = {
    LANGUAGE: ("Jeg har en %s her.", "Den %s er stor.", "%s"),
    LANGUAGE_EN: ("I have a %s here.", "The %s is big.", "%s"),
}

#: sprog -> {ord (med lille): er det et egennavn?}. Svaret er en egenskab ved
#: ordet, ikke ved siden, saa et 40-siders loenbundt betaler kun for det foerste
#: ark. Ryddes af :func:`reset`.
_LOWER_PROPN: dict = {lang: {} for lang in LANGUAGES}


def _propn_when_lowercased(words, lang: str = LANGUAGE) -> frozenset:
    """De ord der stadig tagges som **egennavn** naar de skrives med lille.

    Det er signalet der overlever et dokument uden saetninger. I en tabel er
    hvert ord skrevet med stort, fordi det starter en celle — ikke fordi det er
    et navn — saa versalen siger intet. Skriver man ordet med lille i en
    almindelig saetning, forsvinder den vildledende versal, og modellen svarer
    paa det spoergsmaal der faktisk betyder noget: *er dette ord et navn?*
    ``christensen``, ``horsens`` og ``glostrup`` tagges PROPN; ``gage``,
    ``pension``, ``atp-bdr`` og ``medarbejdernr`` gaar ikke.

    Alle ord proeves i ét dokument, saa hele testen koster én pipelinekoersel
    uanset hvor mange fund siden har.
    """
    seen = {w.lower() for w in words if w[:1].isalpha()}
    if not seen:
        return frozenset()
    cache = _LOWER_PROPN.setdefault(lang, {})
    # Svaret afhaenger kun af ordet selv, og et flersidet dokument stiller det
    # samme spoergsmaal om de samme kolonneoverskrifter paa hver eneste side.
    uniq = sorted(seen - cache.keys())
    if not uniq:
        return frozenset(w for w in seen if cache[w])
    nlp = _engine(lang).nlp_engine.nlp[lang]
    parts, owner, pos = [], {}, 0
    for word in uniq:
        for frame in _NAME_FRAMES.get(lang, _NAME_FRAMES[LANGUAGE]):
            head, _, tail = frame.partition("%s")
            parts.append(head)
            pos += len(head)
            for i in range(len(word)):
                owner[pos + i] = word
            parts.append(word)
            pos += len(word)
            parts.append(tail + "\n")
            pos += len(tail) + 1
    hit = set()
    for tok in nlp("".join(parts)):
        if tok.pos_ == "PROPN" and tok.idx in owner:
            hit.add(owner[tok.idx])
    for word in uniq:
        cache[word] = word in hit
    return frozenset(w for w in seen if cache[w])


#: Vokaler paa dansk og engelsk. ``y`` er med: den baerer stavelsen i ``Nyborg``.
_VOWELS = frozenset("aeiouyæøåAEIOUYÆØÅ")

#: Tegn der aldrig staar i et navn. De optraeder derimod flittigt i OCR-affald
#: fra tabellinjer og rammer.
_NON_NAME_CHARS = frozenset(r"|[]{}<>\_~^*#=+")


def _looks_like_words(value: str) -> bool:
    """Ligner vaerdien overhovedet ord?

    Et OCR'et bilag med en **kopvendt** tabel giver strenge som ``TT TT``,
    ``Sls``, ``fS TT`` og ``[Haloy Sepuvw`` — modellen tagger dem som personer og
    organisationer, fordi de er skrevet med stort og staar alene i en celle.

    To billige regler: ingen af rammetegnene, og **mindst ét** ord skal have en
    vokal. Reglen er bevidst svag i den sidste ende. Den staerkere form — *hvert*
    ord skal have en vokal — blev proevet og forkastet: den fjernede ``HK
    Privat``, og HK er fagforeningen i selve sagen. Et filter der kan skjule en
    part er ubrugeligt her.

    Linjeskift accepteres, fordi et rigtigt navn kan braekke over to linjer.

    Filtret er **ikke** en loesning paa kopvendt OCR — ``Bepsuo`` (som er
    "onsdag" paa hovedet) og ``Jaquisaou`` ("november") har baade vokaler og
    lovlige tegn og slipper igennem. Den rigtige rettelse ligger i
    orienteringsproeven, som i dag vaelger én retning for **hele** siden og
    derfor ikke kan haandtere en side med en drejet tabel midt i.
    """
    if _NON_NAME_CHARS & set(value):
        return False
    ord_ = [t for t in re.split(r"[^\wÆØÅæøå]+", value) if len(t) >= 2]
    if not ord_:
        return True          # kun initialer eller ét bogstav: lad andre regler om det
    return any(_VOWELS & set(t) for t in ord_)


def _drop_name_noise(spans: list, text: str, doc=None, lang: str = LANGUAGE) -> list:
    """Fjern det modellen har tagget som personer, men som ikke er navne.

    Ud over retsinstanser og myndigheder luges her i den stoej en NER-model
    uundgaaeligt producerer paa **tabeller**. En loenseddel eller et kontoudtog
    har ingen saetninger: hver celle bliver sit eget afsnit, modellen mister al
    kontekst og gaetter paa hvert eneste opslagsord. Maalt paa en rigtig
    loenseddel gav side 1 femogtyve fund, hvoraf tre var personoplysninger.

    To signaler skiller navnene fra fagordene:

    * **Ordklassen.** Et navn er et proprium. ``Helligdagstillæg``, ``A-Skat``
      og ``Lønperiode`` tagges NOUN, mens ``Simon Faarkrog Christensen`` tagges
      PROPN hele vejen. Kraeves mindst ét PROPN i spennet, falder fagordene ud.
    * **Cifre.** Modellen tagger jaevnligt beloeb som ORGANIZATION
      (``-1.530,51``, ``-94,67``). Et personnavn indeholder aldrig cifre.
    * **Lemmatiseringen.** Et faellesnavn der blot staar foerst i en celle
      bliver smaaskrevet (``Helligdagstillæg`` -> ``helligdagstillæg``,
      ``Nettoløn`` -> ``nettoløn``), mens et egennavn beholder sin versal
      (``Christensen`` -> ``Christensen``). Maalt: signalet beholdt 14 ud af 14
      rigtige navne og fjernede 7 fagord de andre to signaler lod slippe.
    * **Navnet uden versalen.** De tre signaler ovenfor deler samme svaghed: de
      bygger alle paa et ord der er skrevet med stort, og i en tabel er *alt*
      skrevet med stort. ``Gage``, ``ATP-bdr``, ``Medarbejdernr`` og
      ``Pension Firma PFA`` slap derfor igennem. Skrives ordet med lille i en
      neutral saetning (:func:`_propn_when_lowercased`), forsvinder den
      vildledende versal, og modellen svarer paa det rigtige spoergsmaal.
      Maalt: 16 ud af 16 fagord fra loensedlen faldt ud, og hvert eneste
      flerleddet navn i proevesaettet blev bevaret.

    Et ordforraadsopslag ("er ordet et kendt dansk ord?") blev maalt og
    **forkastet**: det fjerner ganske vist fagordene, men ogsaa ``Anne Jensen``,
    ``Peter Hansen``, ``Horsens`` og ``Glostrup``, som alle staar i modellens
    ordforraad. Filtret maa ikke kunne skjule et navn.

    Prisen er kendt og maalt: et **sjaeldent efternavn der staar helt alene** i
    en tabel — ``Faarkrog`` uden fornavn — genkendes heller ikke med lille og
    falder ud. Optraeder det samme ord i et laengere fund der slap igennem
    (``Simon Faarkrog Christensen``), reddes det af ``_kept_words``.
    """
    index = _name_token_index(doc)
    # Lille-bogstav-proeven er lavet til TABELLER og hoerer kun hjemme dér.
    # I prosa baerer versalen information, og modellen har kontekst at gaette
    # ud fra -- dér koster proeven mere end den giver: den kastede
    # ``Amara Okonkwo`` vaek af en engelsk retsbogsudskrift, fordi hverken
    # ``amara`` eller ``okonkwo`` genkendes som egennavn med lille. Det er
    # undermaskering paa velformet tekst, og det er den farlige retning.
    tabel = not langdetect.is_prose(text)
    candidates, cand_words = [], set()
    if tabel:
        for s in spans:
            if s.entity in (PERSON, ORGANIZATION, LOCATION) and index:
                _, _, words = _name_evidence(index, s.start, s.end)
                cand_words.update(words)
    lower_propn = (_propn_when_lowercased(cand_words, lang)
                   if cand_words else frozenset())
    kept_words = set()
    out = []
    for s in spans:
        if s.entity not in (PERSON, ORGANIZATION, LOCATION):
            out.append(s)
            continue
        value = text[s.start:s.end].strip()
        if _INSTITUTION_RE.match(value):
            continue
        if any(ch.isdigit() for ch in value):
            continue
        if index:
            has_propn, keeps_capital, words = _name_evidence(index, s.start, s.end)
            if not has_propn or not keeps_capital:
                continue
            if lower_propn and not any(w.lower() in lower_propn for w in words):
                candidates.append((s, words))
                continue
            kept_words.update(w.lower() for w in words)
        # "Retten i Glostrup": bynavnet er retskredsen, ikke en personoplysning,
        # og det staar paa hver eneste side i en afgoerelse.
        if s.entity == LOCATION and _COURT_SEAT_RE.search(
                text[max(0, s.start - 40):s.start]):
            continue
        if value.casefold() in _NAME_STOPWORDS:
            continue
        if len(value) < 2:
            continue
        if not _looks_like_words(value):
            continue
        # Et kort ord i rene versaler er en feltetiket ("CPR", "SE"), ikke et
        # navn. Rigtige forkortede navne staar aldrig alene som entitet.
        if len(value) <= 4 and value.isupper():
            continue
        out.append(s)
    # Et ord der indgik i et fund der SLAP igennem, er et navn — ogsaa naar det
    # staar alene. Uden denne redning ville "Faarkrog" i en celle for sig selv
    # falde ud, selv om "Simon Faarkrog Christensen" stod ti linjer laengere nede.
    for s, words in candidates:
        if words and all(w.lower() in kept_words for w in words):
            out.append(s)
    out.sort(key=lambda s: (s.start, s.end))
    return out


def _prune_overlaps(spans: list) -> list:
    """Behold ét fund pr. overlappende omraade.

    Et 8-cifret loeb inde i et CPR-nummer ville ellers ogsaa blive rapporteret
    som telefonnummer og CVR. Vinderen er det laengste spen; ved lige laengde
    afgoer :data:`_PRIORITY`, og derefter scoren.
    """
    def rank(s):
        try:
            pri = _PRIORITY.index(s.entity)
        except ValueError:
            pri = len(_PRIORITY)
        return (-(s.end - s.start), pri, -s.score)

    kept = []
    for s in sorted(spans, key=rank):
        if any(s.start < k.end and s.end > k.start for k in kept):
            continue
        kept.append(s)
    return sorted(kept, key=lambda s: (s.start, s.end))


def normalize_value(entity: str, value: str) -> str:
    """Gruppenoegle for en fundet vaerdi.

    Numeriske identifikatorer reduceres til deres cifre, saa ``010190-1234`` og
    ``0101901234`` er samme gruppe. Personnavne faar danske titler skrellet af,
    saa "Advokat Jensen" og "Jensen" ikke splittes i to raekker brugeren skal
    fravaelge hver for sig.
    """
    v = unicodedata.normalize("NFKC", value or "").strip()
    if entity in (CPR, CPR_LOOSE, PHONE, CVR, ACCOUNT, CREDIT_CARD):
        return re.sub(r"\D", "", v)
    v = re.sub(r"\s+", " ", v).strip(" \t\r\n.,:;!?\"'()[]{}«»")
    if entity == PERSON:
        v = _TITLE_PREFIX_RE.sub("", v).strip()
    return v.casefold()


_TITLE_PREFIX_RE = re.compile(
    r"^(?:advokat(?:fuldmægtig)?|adv\.|dommer|retsformand|anklager|forsvarer|"
    r"hr\.|fru|frk\.|dr\.|prof\.)\s+", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Genkendere der kraever egen logik
# ---------------------------------------------------------------------------
# Klasserne bygges DOVENT: de arver fra Presidio-typer, og Presidio maa ikke
# importeres paa modulniveau (se modulets docstring). ``_classes()`` bygger dem
# foerste gang motoren konstrueres og husker dem bagefter.

_CLASS_CACHE = {}


def _classes():
    if not _CLASS_CACHE:
        _CLASS_CACHE.update(_build_classes())
    return _CLASS_CACHE


def _build_classes():
    from presidio_analyzer import (Pattern, PatternRecognizer, EntityRecognizer,
                                   RecognizerResult)

    class CprRecognizer(PatternRecognizer):
        """Dansk CPR-nummer.

        ``validate_result`` bruger Presidios tre-vaerdi-kontrakt: ``False``
        forkaster, ``True`` loefter til maksimal score, og ``None`` beholder
        moensterets egen score.
        """

        def __init__(self):
            super().__init__(
                supported_entity=CPR, supported_language=LANGUAGE,
                name="DkCprRecognizer",
                patterns=[Pattern("cpr-bindestreg", CPR_STRICT, 0.6),
                          Pattern("cpr-mellemrum", CPR_SPACED, 0.5),
                          Pattern("cpr-bart", CPR_BARE, 0.3)],
                context=["cpr", "cpr-nr", "cprnr", "cpr.nr", "personnummer",
                         "person-nr", "fødselsdato", "født", "fødselsdag"])

        def validate_result(self, pattern_text):
            digits = re.sub(r"\D", "", pattern_text)
            if len(digits) != 10:
                return False
            if not valid_cpr_date(digits):
                return False        # haard afvisning: ugyldig foedselsdato
            if cpr_mod11_ok(digits):
                return True         # modulus 11 passer -> maksimal konfidens
            # Modulus 11 blev AFSKAFFET 1.10.2007. Numre udstedt derefter fejler
            # den lovligt, saa den maa aldrig bruges til at afvise.
            return None

    class CprLooseRecognizer(PatternRecognizer):
        """Ti cifre med vilkaarlige separatorer — kun til OCR'ede sider.

        Tesseract laeser jaevnligt ``0`` som ``O`` eller sætter et mellemrum
        forkert. Uden denne detektor ville ét fejllaest tegn goere et CPR-nummer
        fuldstaendig usynligt for anonymiseringen.
        """

        def __init__(self):
            super().__init__(
                supported_entity=CPR_LOOSE, supported_language=LANGUAGE,
                name="DkCprLooseRecognizer",
                patterns=[Pattern("cpr-loes", CPR_LOOSE_RE.pattern, 0.45)],
                context=["cpr", "personnummer", "født"])

        def validate_result(self, pattern_text):
            digits = re.sub(r"\D", "", pattern_text)
            if len(digits) != 10:
                return False
            # Er datoen gyldig, er det et rigtigt CPR og haandteres af den
            # strenge genkender; her vil vi kun have de tvivlsomme.
            return None if not valid_cpr_date(digits) else False

    class PhoneRecognizer(PatternRecognizer):
        """Dansk telefonnummer — 8 cifre, foerste ciffer 2-9."""

        def __init__(self):
            super().__init__(
                supported_entity=PHONE, supported_language=LANGUAGE,
                name="DkPhoneRecognizer",
                patterns=[Pattern("tlf-intl", PHONE_INTL, 0.7),
                          Pattern("tlf-grupperet", PHONE_GROUPED, 0.5),
                          Pattern("tlf-bart", PHONE_BARE, 0.3)],
                context=["telefon", "tlf", "tlf.", "mobil", "telefonnr",
                         "mobilnr", "kontakt", "træffes"])

        def validate_result(self, pattern_text):
            digits = re.sub(r"\D", "", pattern_text)
            if digits.startswith("45") and len(digits) == 10:
                digits = digits[2:]
            elif digits.startswith("0045") and len(digits) == 12:
                digits = digits[4:]
            if len(digits) != 8:
                return False
            return None if digits[0] not in "01" else False

    class CvrRecognizer(PatternRecognizer):
        """CVR-nummer. Modulus 11 er **stadig** gaeldende, saa den afviser haardt."""

        def __init__(self):
            super().__init__(
                supported_entity=CVR, supported_language=LANGUAGE,
                name="DkCvrRecognizer",
                patterns=[Pattern("cvr", CVR_RE, 0.1)],
                context=["cvr", "cvr-nr", "cvrnr", "se-nr", "se-nummer",
                         "momsnr", "virksomhed", "p-nr"])

        def validate_result(self, pattern_text):
            digits = re.sub(r"\D", "", pattern_text)
            return True if cvr_mod11_ok(digits) else False

    class PostalRecognizer(PatternRecognizer):
        def __init__(self):
            super().__init__(
                supported_entity=POSTAL, supported_language=LANGUAGE,
                name="DkPostalRecognizer",
                patterns=[Pattern("postnr+by", POSTAL_RE, 0.45)],
                context=["adresse", "postnr", "postnummer", "by", "bopæl"])

        def validate_result(self, pattern_text):
            return postal_match(pattern_text)

        def analyze(self, text, entities, nlp_artifacts=None, regex_flags=None):
            results = super().analyze(text, entities, nlp_artifacts, regex_flags)
            # Et match valideringen afviste, kan have et gyldigt postnummer + by
            # i starten -- se postal_prefix. Presidio proever ikke selv kortere.
            flags = regex_flags or self.global_regex_flags
            for m in re.finditer(POSTAL_RE, text, flags):
                if postal_match(m.group(0)):
                    continue
                kort = postal_prefix(m.group(0))
                if not kort:
                    continue
                expl = self.build_regex_explanation(
                    self.name, "postnr+by-kort", POSTAL_RE, 0.45, True, flags)
                expl.score = EntityRecognizer.MAX_SCORE
                results.append(RecognizerResult(
                    entity_type=POSTAL, start=m.start(),
                    end=m.start() + len(kort), score=EntityRecognizer.MAX_SCORE,
                    analysis_explanation=expl,
                    recognition_metadata={
                        RecognizerResult.RECOGNIZER_NAME_KEY: self.name,
                        RecognizerResult.RECOGNIZER_IDENTIFIER_KEY: self.id,
                    }))
            return results

    class CaseNoRecognizer(EntityRecognizer):
        """Sags- og journalnumre.

        Er en ``EntityRecognizer`` og ikke en ``PatternRecognizer``, fordi det
        nyttige spen ved ``"j.nr.: 2024-0012345"`` er **capture-gruppen** —
        ``PatternRecognizer`` rapporterer altid hele matchet inkl. noegleordet.
        """

        def __init__(self):
            super().__init__(supported_entities=[CASE_NO],
                             supported_language=LANGUAGE,
                             name="DkCaseNoRecognizer")

        def load(self):
            return None

        def analyze(self, text, entities, nlp_artifacts=None):
            results = []
            for m in CASE_COURT_RE.finditer(text):
                results.append(RecognizerResult(CASE_NO, m.start(), m.end(), 0.6))
            for m in CASE_KEYED_RE.finditer(text):
                results.append(RecognizerResult(CASE_NO, m.start(1), m.end(1), 0.55))
            return results

    return {"Cpr": CprRecognizer, "CprLoose": CprLooseRecognizer,
            "Phone": PhoneRecognizer, "Cvr": CvrRecognizer,
            "Postal": PostalRecognizer, "CaseNo": CaseNoRecognizer}
