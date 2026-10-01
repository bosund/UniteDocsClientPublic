"""Datoer i filnavne -- til Power-sortering.

GUI-frit (ingen Qt), saa det testes headless fra ``tests/test_suite.py``.

To veje til en :class:`DateRule`:

* :func:`detect_rule` proever en raekke kendte formater (``31062022``,
  ``2022-06-31``, ``31. juni 2022``, ``June 30, 2022`` ...) mod ALLE filnavnene
  og vaelger det der giver en gyldig dato i flest af dem. Det er hele pointen
  med at se paa samlingen frem for det enkelte navn: ``01062022`` kan vaere
  baade dd-mm og mm-dd, men ``31062022`` i naboen kan kun vaere dd-mm.
* :func:`rule_from_marks` bygger reglen af de tegn brugeren selv har markeret
  i ét eksempel ("Manuel").

Maanedsnavne genkendes paa alle appens otte sprog, fulde og forkortede
(``juni``, ``jun``, ``june``, ``juin``, ``junio`` ...).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field

# --- Maanedsnavne -----------------------------------------------------------
# Een liste pr. maaned: fulde navne og forkortelser paa da/en/de/es/fr/nb/nl/sv.
# Ingen forkortelse betyder to forskellige maaneder paa to sprog, saa listen kan
# flades ud til eet opslag uden at kende filnavnets sprog. Forkortelser der
# ogsaa er almindelige ord (es "ago", it/es "set", nb/fr "des") er udeladt:
# "Relevé des comptes 2022" er ikke december.
_MONTH_WORDS = (
    ("januar", "january", "januari", "jänner", "janvier", "janv", "enero", "ene",
     "jan", "jän"),
    ("februar", "february", "februari", "février", "fevrier", "févr", "fevr",
     "febrero", "feb", "fév", "fev"),
    ("marts", "march", "märz", "maerz", "mars", "maart", "marzo", "mär", "mrz",
     "mrt", "mar"),
    ("april", "avril", "abril", "apr", "avr", "abr"),
    ("maj", "may", "mai", "mei", "mayo"),
    ("juni", "june", "juin", "junio", "jun"),
    ("juli", "july", "juillet", "juil", "julio", "jul"),
    ("august", "augusti", "augustus", "août", "aout", "agosto", "aug"),
    ("september", "septiembre", "setiembre", "septembre", "sept", "sep"),
    ("oktober", "october", "octubre", "octobre", "okt", "oct"),
    ("november", "noviembre", "novembre", "nov"),
    ("december", "dezember", "desember", "diciembre", "décembre", "decembre",
     "dec", "dez", "dic", "déc"),
)

MONTHS: dict = {w: i for i, words in enumerate(_MONTH_WORDS, 1) for w in words}

# Laengste foerst, ellers vinder "jun" over "juni" i alternationen og efterlader
# et "i" der goer at ordgraensen ikke passer.
_MONTH_ALT = "|".join(sorted((re.escape(w) for w in MONTHS), key=len, reverse=True))
# Ikke klistret til andre BOGSTAVER -- men gerne til cifre ("juni2022").
_MONTH = r"(?<![^\W\d_])(?P<m>%s)\.?(?![^\W\d_])" % _MONTH_ALT

_SEP = r"[-._ ]"      # det der staar mellem felterne i et filnavn
_GAP = r"[-._ ,]*"    # mellem et maanedsnavn og et tal


def month_number(word: str) -> int | None:
    """``"Juni"`` / ``"jun."`` / ``"June"`` -> 6. ``None`` hvis ukendt."""
    return MONTHS.get(word.strip().rstrip(".").casefold())


# --- Aar -----------------------------------------------------------------
def _year(text: str, today: datetime.date) -> int | None:
    y = int(text)
    if len(text) == 2:
        # Pivot: to-cifrede aar op til fem aar frem regnes som 20xx.
        pivot = (today.year + 5) % 100
        y += 2000 if y <= pivot else 1900
    if not 1900 <= y <= today.year + 10:
        return None
    return y


def _make_date(y, m, d, today) -> datetime.date | None:
    year = _year(y, today) if isinstance(y, str) else y
    if year is None:
        return None
    if isinstance(m, str):
        month = int(m) if m.isdigit() else month_number(m)
    else:
        month = m
    day = int(d) if d else 1
    if not month:
        return None
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


# --- Regel ---------------------------------------------------------------
@dataclass
class DateRule:
    """Et moenster der finder EEN dato i et filnavn.

    ``tokens`` beskriver formatet til visning: ``[("d", 2), "-", ("m", 2), ...]``
    hvor et felt er ``(bogstav, bredde)`` og bredde 0 = maanedsnavn/variabel.
    UI'et oversaetter bogstaverne; modulet her er sprogfrit.
    """

    key: str
    regex: "re.Pattern"
    tokens: list = field(default_factory=list)
    # "dm" hvis dag staar foer maaned, "md" omvendt, "" hvis entydigt.
    order: str = ""
    has_day: bool = True

    def parse(self, name: str, today: datetime.date | None = None
              ) -> datetime.date | None:
        """Foerste GYLDIGE dato i ``name`` -- et kundenummer foran datoen maa
        ikke skygge for den."""
        today = today or datetime.date.today()
        for m in self.regex.finditer(name):
            g = m.groupdict()
            found = _make_date(g.get("y"), g.get("m"), g.get("d"), today)
            if found is not None:
                return found
        return None


def _rx(body: str) -> "re.Pattern":
    return re.compile(body, re.IGNORECASE)


_D2, _M2 = r"(?P<d>\d{2})", r"(?P<m>\d{2})"
_D, _Mn = r"(?P<d>\d{1,2})", r"(?P<m>\d{1,2})"
_Y4, _Y2, _Y = r"(?P<y>\d{4})", r"(?P<y>\d{2})", r"(?P<y>\d{4}|\d{2})"
_ND0, _ND1 = r"(?<!\d)", r"(?!\d)"          # ikke midt i et laengere tal
_S = r"(?P<s>%s)" % _SEP                     # separatoren ...
_S2 = r"(?P=s)"                              # ... og den samme igen


def _candidates() -> list:
    """Alle kendte formater, i PRIORITET: ved lige mange fund vinder den foerste.

    Dansk dag-maaned kommer foer amerikansk maaned-dag; hele datoer foer
    maaned-aar."""
    c = []

    def add(key, body, tokens, order="", has_day=True):
        c.append(DateRule(key, _rx(body), tokens, order, has_day))

    add("ddmmyyyy", _ND0 + _D2 + _M2 + _Y4 + _ND1,
        [("d", 2), ("m", 2), ("y", 4)], "dm")
    add("yyyymmdd", _ND0 + _Y4 + _M2 + _D2 + _ND1,
        [("y", 4), ("m", 2), ("d", 2)])
    add("d-m-y", _ND0 + _D + _S + _Mn + _S2 + _Y + _ND1,
        [("d", 2), "-", ("m", 2), "-", ("y", 4)], "dm")
    add("y-m-d", _ND0 + _Y4 + _S + _Mn + _S2 + _D + _ND1,
        [("y", 4), "-", ("m", 2), "-", ("d", 2)])
    add("d-month-y", _ND0 + _D + r"\.?" + _GAP + _MONTH + _GAP + _Y + _ND1,
        [("d", 2), " ", ("m", 0), " ", ("y", 4)])
    add("month-d-y", _MONTH + _GAP + _D + r"(?:st|nd|rd|th)?" + _GAP + _Y4 + _ND1,
        [("m", 0), " ", ("d", 2), ", ", ("y", 4)])
    add("y-month-d", _ND0 + _Y4 + _GAP + _MONTH + _GAP + _D + _ND1,
        [("y", 4), " ", ("m", 0), " ", ("d", 2)])
    add("ddmmyy", _ND0 + _D2 + _M2 + _Y2 + _ND1,
        [("d", 2), ("m", 2), ("y", 2)], "dm")
    add("yymmdd", _ND0 + _Y2 + _M2 + _D2 + _ND1,
        [("y", 2), ("m", 2), ("d", 2)])
    add("mmddyyyy", _ND0 + _M2 + _D2 + _Y4 + _ND1,
        [("m", 2), ("d", 2), ("y", 4)], "md")
    add("m-d-y", _ND0 + _Mn + _S + _D + _S2 + _Y + _ND1,
        [("m", 2), "-", ("d", 2), "-", ("y", 4)], "md")
    add("mmddyy", _ND0 + _M2 + _D2 + _Y2 + _ND1,
        [("m", 2), ("d", 2), ("y", 2)], "md")
    # Kun maaned og aar: dagen saettes til den 1.
    add("month-y", _MONTH + _GAP + _Y4 + _ND1,
        [("m", 0), " ", ("y", 4)], has_day=False)
    add("y-month", _ND0 + _Y4 + _GAP + _MONTH,
        [("y", 4), " ", ("m", 0)], has_day=False)
    add("m-y", _ND0 + _Mn + _SEP + _Y4 + _ND1,
        [("m", 2), "-", ("y", 4)], has_day=False)
    add("y-m", _ND0 + _Y4 + _SEP + _Mn + _ND1,
        [("y", 4), "-", ("m", 2)], has_day=False)
    add("yyyymm", _ND0 + _Y4 + _M2 + _ND1,
        [("y", 4), ("m", 2)], has_day=False)
    add("mmyyyy", _ND0 + _M2 + _Y4 + _ND1,
        [("m", 2), ("y", 4)], has_day=False)
    return c


CANDIDATES = _candidates()


@dataclass
class Detection:
    """Resultatet af :func:`detect_rule`."""

    rule: "ChainRule"
    hits: int          # antal navne der gav en dato
    total: int


class ChainRule:
    """Den valgte regel + de forenelige reserver, i raekkefoelge.

    Et navn der ikke passer det dominerende format, proeves mod de naeste --
    men aldrig mod et der vender dag og maaned om i forhold til det valgte.
    ``04052022`` i en samling af dd-mm-navne maa ikke laeses som 5. april, bare
    fordi den heller ikke passede noget andet.
    """

    def __init__(self, rules: list):
        self.rules = list(rules)

    @property
    def primary(self) -> DateRule:
        return self.rules[0]

    @property
    def tokens(self) -> list:
        return self.primary.tokens

    def parse(self, name: str, today=None) -> datetime.date | None:
        for r in self.rules:
            found = r.parse(name, today)
            if found is not None:
                return found
        return None


def detect_rule(names, today: datetime.date | None = None) -> "Detection | None":
    """Find det datoformat der passer flest af ``names`` (filnavne uden sti).

    ``None`` hvis intet format giver en eneste dato."""
    names = list(names)
    today = today or datetime.date.today()
    scored = []
    for prio, rule in enumerate(CANDIDATES):
        hits = sum(1 for n in names if rule.parse(n, today) is not None)
        if hits:
            # Flest fund; ved lige vinder en hel dato over maaned-aar, og
            # derefter listens prioritet.
            scored.append((-hits, not rule.has_day, prio, rule))
    if not scored:
        return None
    scored.sort(key=lambda t: t[:3])
    primary = scored[0][3]
    chain = [primary] + [r for *_k, r in scored[1:]
                         if not (primary.order and r.order and r.order != primary.order)]
    rule = ChainRule(chain)
    hits = sum(1 for n in names if rule.parse(n, today) is not None)
    return Detection(rule, hits, len(names))


# --- Manuel: regel ud fra markerede tegn -----------------------------------
class MarkError(ValueError):
    """Markeringerne kan ikke blive til en regel. ``code`` er en stabil noegle
    som UI'et oversaetter; teksten her er kun til loggen."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def rule_from_marks(example: str, marks: dict) -> DateRule:
    """Byg en regel af de tegn brugeren har markeret i ``example``.

    ``marks`` er ``{"y": (start, slut), "m": (...), "d": (...)}``; dag er
    valgfri (mangler den, bliver det den 1.). Moensteret er kun det markerede
    omraade plus de tegn der staar IMELLEM felterne -- teksten foran og bagved
    varierer typisk fra fil til fil og maa ikke vaere en del af det. Til
    gengaeld maa omraadet ikke ligge midt i et laengere tal eller ord.
    """
    if "y" not in marks or "m" not in marks:
        raise MarkError("missing")
    spans = sorted(((k, int(a), int(b)) for k, (a, b) in marks.items()),
                   key=lambda t: t[1])
    for k, a, b in spans:
        if not 0 <= a < b <= len(example):
            raise MarkError("empty", k)
    for (_k1, _a1, b1), (_k2, a2, _b2) in zip(spans, spans[1:]):
        if a2 < b1:
            raise MarkError("overlap")

    def is_digit(i):
        return 0 <= i < len(example) and example[i].isdigit()

    parts, tokens = [], []
    start, end = spans[0][1], spans[-1][2]
    first_digit = example[start].isdigit()
    last_digit = example[end - 1].isdigit()
    if first_digit and not is_digit(start - 1):
        parts.append(r"(?<!\d)")
    elif not first_digit:
        parts.append(r"(?<![^\W\d_])")

    for i, (k, a, b) in enumerate(spans):
        text = example[a:b]
        if i:
            gap = example[spans[i - 1][2]:a]
            if gap:
                if any(ch.isalnum() for ch in gap):
                    parts.append(re.escape(gap))
                else:
                    parts.append(r"[\W_]+")
                tokens.append(gap)
        # Et felt maa variere i bredde (1 eller 2 cifre) kun naar der er et
        # ikke-ciffer paa BEGGE sider -- ellers er bredden det eneste der
        # skiller "3162022" fra "31062022".
        free = not is_digit(a - 1) and not is_digit(b)
        if k == "y":
            if not text.isdigit() or len(text) not in (2, 4):
                raise MarkError("year", text)
            parts.append(r"(?P<y>\d{%d})" % len(text))
            tokens.append(("y", len(text)))
        elif text.isdigit():
            if len(text) > 2:
                raise MarkError("month" if k == "m" else "day", text)
            parts.append(r"(?P<%s>\d{1,2})" % k if free
                         else r"(?P<%s>\d{%d})" % (k, len(text)))
            tokens.append((k, 2))
        elif k == "m" and month_number(text) is not None:
            parts.append(r"(?P<m>[^\W\d_]+)\.?")
            tokens.append(("m", 0))
        else:
            raise MarkError("month" if k == "m" else "day", text)

    if last_digit and not is_digit(end):
        parts.append(r"(?!\d)")
    elif not last_digit:
        parts.append(r"(?![^\W\d_])")
    return DateRule("manual", _rx("".join(parts)), tokens, has_day="d" in marks)
