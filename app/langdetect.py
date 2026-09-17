"""Er der engelsk i denne tekst?

Modulet er bevidst **ensidigt**. Det svarer ikke paa "hvilket sprog er dette?",
men paa det langt lettere spoergsmaal "er der engelsk her?". Dansk er
udgangspunktet og koeres altid; engelsk laegges oven i, naar der er positivt
bevis for det (se ``docs/anonymization.md``).

Hvorfor der skal **bevis** til
------------------------------
Begge fejlretninger er maalt, og de er ikke lige slemme:

* **For lidt engelsk.** Den danske model alene fandt **12 af 18** navne i
  engelsk tekst — ``Okonkwo``, ``Tanaka``, ``Haddad``, ``Pryce`` og ``Marsh``
  gik tabt. Det er undermaskering: usynligt, og et navn slipper ud.
* **For meget engelsk.** Den engelske model paa en dansk loenseddel tog fundene
  fra 7 til 14: ``Saldi``, ``ATP-bdr``, ``Medarbejdernr`` og ``Pension Firma``
  kom tilbage som personer og organisationer. Det engelske stoejfilter kan ikke
  afvise danske fagord, for de er ikke engelske faellesnavne.

Ingen af dem er gratis, saa taersklen skal ligge et sted hvor **begge** sider har
luft. Det gav den maaling der staar i :data:`EN_DA_RATIO`.

Motoren
-------
``fasttext`` med Facebooks ``lid.176.ftz`` (176 sprog, 0,9 MB). Beslutningen
tages paa **forholdet** ``p(en) / p(da)``, ikke paa modellens top-1: en dansk
adresseblok uden saetninger fik ``en 0.34`` som top-1, men ``p(da)`` var 0,12, og
forholdet dermed 2,8 — langt under taersklen.

En haandskreven ordliste blev proevet foerst og **forkastet**. Den var perfekt
paa prosa (0 % engelske funktionsord i al dansk tekst mod 37-48 % i al engelsk),
men fejlede 4 ud af 5 paa engelsk **uden** funktionsord: punktlister,
overskrifter i en kontraktbilagsfortegnelse, journalnotater og kontoudtog. Det
er praecis den slags sider der ligger i et sagsbundt. Listerne staar tilbage som
noedplan, hvis modellen mangler.

Modulet er GUI-frit og importerer ikke ``fasttext`` paa modulniveau.
"""

import functools
import re

from .logging_config import get_logger
from .utils import resource_path

logger = get_logger(__name__)

#: Facebooks sprog-id-model. Den komprimerede ``.ftz`` er 0,9 MB mod 125 MB for
#: ``.bin`` og taber intet maalbart her. Licens: **CC BY-SA 3.0** — se
#: ``app/credits.py``. Hentes med ``python client/fetch_model.py``.
LID_MODEL = "lid.176.ftz"

#: ``p(en) / p(da)`` skal mindst vaere saa stor.
#:
#: Maalt paa 20 tekster: de danske laa paa **0,0-2,8**, de engelske paa
#: **13-48615**. Der er ingen maalepunkter imellem. Vaerdien er det geometriske
#: midtpunkt i det tomme baand — 2,2x luft til den naermeste danske og 2,2x til
#: den naermeste engelske — frem for en ende af det, saa den ikke er tunet til
#: netop de tyve tekster.
#:
#: De to naermeste danske (en adresseblok paa 2,8 og en raekke forkortelser paa
#: 1,6) er desuden dem hvor en fejl koster mindst: paatvinges den engelske model
#: adresseblokken, er resultatet **identisk** — seks fund, samme spans. Den dyre
#: fejl er loensedlen, og den ligger paa 0,02, fire stoerrelsesordener vaek.
EN_DA_RATIO = 6.0

#: Under saa faa ord er ethvert svar stoej. En enkelt tabelcelle
#: (``"Beskrivelse"``) fik ``no 0.37, sv 0.29`` af modellen — selvsikkert vroevl.
#:
#: Tallet er **maalt**, ikke valgt. Foerste udgave stod paa 25 og afviste fire
#: rigtige engelske sider (punktliste, kontraktoverskrifter, journalnotat,
#: kontoudtog) foer modellen overhovedet blev spurgt — spaerren, ikke modellen,
#: var fejlen.
#:
#: Nedad er graensen sat af **dansk tabelsprog**. Paa fulde sider ligger de
#: danske forhold paa 0,0-2,8 mod taersklen 6,0, men et kort fragment af rene
#: loenartsnavne (``Gage``, ``Sats``, ``Beløb``, ``Saldi``, ``Nettoløn`` — ni
#: tokens) maaler **3,8**. Det er stadig under, men kun med faktor 1,6, og
#: fejlen dér er dyr: den engelske model paa en dansk loenseddel bringer
#: fagordene tilbage som navne. Under et dusin ord er modellen altsaa upaalidelig
#: praecis hvor det koster mest, saa dér afstaar vi. Prisen er at et engelsk
#: fragment paa faerre end tolv ord ikke findes — og saa lidt tekst har naesten
#: intet at maskere.
MIN_TOKENS = 12

_TOKEN_RE = re.compile(r"[a-zA-ZæøåÆØÅ][a-zA-ZæøåÆØÅ']*")

# --- noedplan: ordlisterne fra foerste udgave --------------------------------
# Bruges kun naar lid-modellen mangler. ``at``, ``for``, ``to`` og ``by`` er
# bevidst udeladt af den engelske liste: de er alle almindelige danske ord.
_EN_WORDS = frozenset("""
the and of in is that with this be are was were shall which from have has been
not any all such said will would may must other than their there upon herein
hereby between under above who whom whose when where while these those it its
""".split())

_DA_WORDS = frozenset("""
og ikke til at som med af har kan skal ved om eller der fra paa på det den er
en et var blev efter over under mellem samt hvis fordi men også ingen hver
""".split())

_FALLBACK_EN_RATIO = 0.06
_FALLBACK_MARGIN = 2.0


@functools.lru_cache(maxsize=1)
def model_path():
    """Stien til ``lid.176.ftz``, eller None hvis den ikke er installeret."""
    try:
        path = resource_path("models") / LID_MODEL
        return path if path.exists() else None
    except Exception as e:                       # pragma: no cover - defensivt
        logger.info("Kunne ikke slaa sprogdetektoren op: %s", e)
        return None


@functools.lru_cache(maxsize=1)
def _detector():
    """Den indlaeste fasttext-model, eller None.

    Importen ligger herinde og ikke paa modulniveau: ``anonymize`` og ``pii``
    skal kunne importeres i en installation hvor modellen mangler.
    """
    path = model_path()
    if path is None:
        return None
    try:
        import fasttext
        return fasttext.load_model(str(path))
    except Exception as e:
        logger.warning("Sprogdetektoren kunne ikke loades: %s", e)
        return None


def available() -> bool:
    """Er detektoren baade installeret **og** indlaeselig?

    ``model_path()`` svarer kun paa om FILEN ligger der. Mangler
    ``fasttext``-modulet -- fx i et venv hvor kun en delmaengde af
    ``requirements.txt`` er installeret -- ligger filen stadig, men
    :func:`english_score` falder tavst tilbage paa ordlisterne og svarer
    ``ratio=None``.

    De to tilstande skal kunne skelnes udefra. Ellers maaler en proeve
    noedplanen i den tro at den maaler modellen, og fejler med et tal der ikke
    peger paa noget ("forhold=None"). Det skete.
    """
    return _detector() is not None


def reset() -> None:
    """Slip modellen igen (bruges af tests)."""
    _detector.cache_clear()
    model_path.cache_clear()


def _word_score(text: str) -> dict:
    tokens = _TOKEN_RE.findall(text or "")
    n = len(tokens)
    if not n:
        return {"tokens": 0, "en": 0, "da": 0, "en_ratio": 0.0, "prose": 0.0}
    lower = [t.lower() for t in tokens]
    en = sum(1 for t in lower if t in _EN_WORDS)
    da = sum(1 for t in lower if t in _DA_WORDS)
    return {"tokens": n, "en": en, "da": da, "en_ratio": en / n,
            "prose": max(en, da) / n}


#: Over denne andel funktionsord regnes teksten for **prosa** — altsaa noget med
#: saetninger, hvor et stort begyndelsesbogstav faktisk betyder noget.
#:
#: Maalt: en loenseddelside har 2-3 % funktionsord, en tabeloverskriftsblok 0 %,
#: mens dansk og engelsk prosa ligger paa 29-48 %. Der er intet imellem. Ti
#: procent ligger i det tomme baand.
PROSE_MIN = 0.10


def is_prose(text: str) -> bool:
    """Har teksten saetninger, eller er den en tabel?

    Bruges af :func:`pii._drop_name_noise` til at afgoere om versalen paa et ord
    er informativ. I en tabel starter hvert ord en celle og er derfor skrevet
    med stort uanset hvad; i prosa er en versal midt i en saetning et signal.
    """
    s = _word_score(text)
    return s["tokens"] >= MIN_TOKENS and s["prose"] >= PROSE_MIN


def english_score(text: str) -> dict:
    """Tallene bag beslutningen. Bruges af testene og til fejlsoegning.

    ``ratio`` er ``p(en) / p(da)`` fra lid-modellen, eller ``None`` naar den
    mangler og noedplanen bruges.
    """
    out = _word_score(text)
    out["p_en"] = out["p_da"] = 0.0
    out["ratio"] = None
    det = _detector()
    if det is None or out["tokens"] < MIN_TOKENS:
        return out
    try:
        # Modellen vil have ét linjeskiftsfrit input; afsnitsdelingen fra
        # words_to_text ville ellers blive laest som flere dokumenter.
        labels, probs = det.predict(" ".join((text or "").split()), k=-1)
    except Exception as e:                       # pragma: no cover - defensivt
        logger.warning("Sprogdetektionen fejlede: %s", e)
        return out
    by_lang = {l.replace("__label__", ""): p for l, p in zip(labels, probs)}
    out["p_en"] = float(by_lang.get("en", 0.0))
    out["p_da"] = float(by_lang.get("da", 0.0))
    out["ratio"] = out["p_en"] / out["p_da"] if out["p_da"] > 1e-9 else float("inf")
    return out


def looks_english(text: str) -> bool:
    """Er der nok engelsk til at det betaler sig at koere den engelske model?

    Falsk betyder **ikke** "dette er dansk" — det betyder "der er ikke bevis for
    engelsk". Det er den rigtige forsigtighed: koerer den engelske model paa en
    dansk loenseddel, kommer fagordene tilbage som navne.
    """
    s = english_score(text)
    if s["tokens"] < MIN_TOKENS:
        return False
    if s["ratio"] is not None:
        return s["ratio"] >= EN_DA_RATIO
    # Noedplan uden model: ordlisterne. De misser engelsk uden funktionsord,
    # men de tager aldrig fejl den anden vej, og det er den vigtige.
    if s["en_ratio"] < _FALLBACK_EN_RATIO:
        return False
    return s["en"] >= s["da"] * _FALLBACK_MARGIN
