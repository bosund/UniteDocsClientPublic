#!/usr/bin/env python3
"""Vagt og arbejdsredskab for oversættelserne.

Hvorfor filen findes
--------------------
``pybabel extract`` er god til at finde strengene, og ``pybabel update`` er god
til at flette dem ind. Det de **ikke** kan er at afgøre om en oversættelse er
rigtig — og som standard *gætter* ``update`` ved at kopiere den mest
strenglignende eksisterende oversættelse og sætte ``#, fuzzy`` på. Målt på denne
kodebase gav det bl.a.:

    "Kopiér tekst"  -> "Enter text:"
    "Valgte sider"  -> "Delete page"
    "Navneoversigt" -> "Heading"
    "Oprettet: %s"  -> "Downloaded %s"

38 sådanne gæt pr. sprog, og ingen af dem var rigtige. Babel kompilerer ganske
vist ikke fuzzy-poster ind i ``.mo`` (målt: ``write_mo`` har ``use_fuzzy=False``
som standard), så de viser sig som **dansk tekst midt i en engelsk flade** —
lige tavst nok til at nå en udgivelse. Og fjerner nogen flaget uden at læse
teksten, bliver gættet til en rigtig oversættelse.

Derfor to ting: ``update_locales.py`` kører nu med ``--no-fuzzy-matching``, så
en ny streng kommer ud som **tom** i stedet for som plausibelt vrøvl, og denne
fil er den gate der siger fra, hvis noget mangler.

Brug
----
    python client/check_locales.py                 # kontrollér alt (exit 1 ved fejl)
    python client/check_locales.py --list u.json   # skriv de manglende ud
    python client/check_locales.py --apply u.json  # læg oversættelserne ind
    python client/check_locales.py --fill-danish   # dansk msgstr = msgid

``--list``/``--apply`` er dét der gør en oversættelsesrunde til en opgave der
kan gentages: listen er ren JSON med ét felt pr. (streng, sprog), og ``--apply``
validerer hver eneste indsat tekst mod msgid'et, før den skriver. En manglende
``%s`` eller et tabt linjeskift bliver afvist i stedet for at ende i en dialog.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from babel.messages.pofile import read_po, write_po

CLIENT = Path(__file__).resolve().parent
LOCALES = CLIENT / "app" / "locales"

#: Kilden er skrevet på dansk, så det danske katalog er identitet.
SOURCE_LANG = "da"
LANGS = ("da", "en", "de", "es", "fr", "nb_NO", "nl", "sv")

#: printf-pladsholdere. ``%%`` er et literalt procenttegn og tælles ikke med.
_PRINTF = re.compile(r"%%|%\([^)]*\)[-+ #0]*[\d*]*(?:\.\d+)?[a-zA-Z]"
                     r"|%[-+ #0]*[\d*]*(?:\.\d+)?[a-zA-Z]")
#: ``str.format``-pladsholdere. Bruges ikke i dag, men et tabt ``{navn}`` ville
#: være lige så usynligt som et tabt ``%s``.
_BRACE = re.compile(r"\{[^{}]*\}")


def placeholders(text: str) -> list:
    """Pladsholderne i ``text``, sorteret. Rækkefølgen i sætningen må gerne
    ændre sig ved oversættelse — mængden må ikke."""
    return sorted([m for m in _PRINTF.findall(text) if m != "%%"]
                  + _BRACE.findall(text))


def po_path(lang: str) -> Path:
    return LOCALES / lang / "LC_MESSAGES" / "messages.po"


def load(lang: str):
    with open(po_path(lang), encoding="utf-8") as f:
        return read_po(f, locale=lang)


def save(lang: str, cat) -> None:
    with open(po_path(lang), "wb") as f:
        write_po(f, cat, width=76)


def _entries(cat):
    """Poster med et rigtigt msgid. Header-posten (tomt msgid) springes over."""
    for m in cat:
        if m.id:
            yield m


def check_message(msgid: str, value: str) -> list:
    """Alt der kan gøre en oversættelse forkert uden at nogen opdager det."""
    fejl = []
    if placeholders(value) != placeholders(msgid):
        fejl.append("pladsholderne passer ikke (%s mod %s)"
                    % (placeholders(value), placeholders(msgid)))
    if value.count("\n") != msgid.count("\n"):
        fejl.append("antal linjeskift passer ikke (%d mod %d)"
                    % (value.count("\n"), msgid.count("\n")))
    # Et afsluttende "…" på en knap betyder "åbner en dialog". Det er semantik,
    # ikke pynt, og det forsvinder let i en oversættelse.
    if msgid.endswith("…") and not value.endswith("…"):
        fejl.append("det afsluttende '…' mangler")
    # Førende/afsluttende mellemrum bærer layout i sammensat tekst.
    if (msgid[:1] == " ") != (value[:1] == " "):
        fejl.append("førende mellemrum passer ikke")
    if (msgid[-1:] == " ") != (value[-1:] == " "):
        fejl.append("afsluttende mellemrum passer ikke")
    return fejl


def source_msgids() -> set:
    """Strengene som de står i koden LIGE NU.

    Udtrækket køres direkte gennem Babels API i stedet for ``pybabel extract``,
    så gaten ikke afhænger af en underproces — og så den kan sammenlignes med
    ``.pot``-filen. Det er dét der fanger den hyppigste fejl af alle: at nogen
    har skrevet ``_("ny tekst")`` og aldrig kørt ``update_locales.py``.
    """
    from babel.messages.extract import extract_from_dir

    ud = set()
    for _fil, _linje, message, _kommentar, _ctx in extract_from_dir(
            str(CLIENT / "app"),
            method_map=[("**.py", "python")],
            keywords={"_": None, "N_": None}):     # N_: undo-etiketter
        if isinstance(message, str):
            ud.add(message)
        elif message and isinstance(message[0], str):
            ud.add(message[0])
    return ud


def check_all() -> list:
    problemer = []
    kilde = {m.id: m for m in _entries(load(SOURCE_LANG))}

    i_koden = source_msgids()
    i_katalog = set(kilde)
    for msgid in sorted(i_koden - i_katalog):
        problemer.append("KODEN har en streng katalogerne ikke kender: %r "
                         "(kør update_locales.py)" % msgid)

    for lang in LANGS:
        cat = load(lang)
        ids = set()
        for m in _entries(cat):
            msgid = m.id if isinstance(m.id, str) else m.id[0]
            ids.add(msgid)
            if m.fuzzy:
                problemer.append("%s: FUZZY %r -> %r" % (lang, msgid, m.string))
                continue
            if not m.string:
                problemer.append("%s: UOVERSAT %r" % (lang, msgid))
                continue
            value = m.string if isinstance(m.string, str) else m.string[0]
            for f in check_message(msgid, value):
                problemer.append("%s: %s: %r -> %r" % (lang, f, msgid, value))
        mangler = set(kilde) - ids
        for msgid in sorted(mangler):
            problemer.append("%s: strengen findes slet ikke i katalogget: %r "
                             "(kør update_locales.py)" % (lang, msgid))
    return problemer


def missing() -> dict:
    """``{msgid: {sprog: ""}}`` for alt der mangler eller er fuzzy."""
    ud = {}
    for lang in LANGS:
        if lang == SOURCE_LANG:
            continue
        for m in _entries(load(lang)):
            if m.string and not m.fuzzy:
                continue
            msgid = m.id if isinstance(m.id, str) else m.id[0]
            ud.setdefault(msgid, {})[lang] = ""
    return ud


def apply_table(table: dict) -> int:
    """Læg oversættelserne ind. Validerer FØR der skrives til nogen fil."""
    fejl, planlagt = [], {}
    for lang in LANGS:
        if lang == SOURCE_LANG:
            continue
        cat = load(lang)
        aendringer = []
        for msgid, per_lang in table.items():
            if lang not in per_lang:
                continue
            value = per_lang[lang]
            msg = cat.get(msgid)
            if msg is None:
                fejl.append("%s: ukendt streng: %r" % (lang, msgid))
                continue
            if not value.strip():
                fejl.append("%s: tom oversættelse: %r" % (lang, msgid))
                continue
            for f in check_message(msgid, value):
                fejl.append("%s: %s: %r -> %r" % (lang, f, msgid, value))
            aendringer.append((msg, value))
        planlagt[lang] = (cat, aendringer)

    if fejl:
        for f in fejl:
            print("  FEJL:", f)
        return 0

    lagt_ind = 0
    for lang, (cat, aendringer) in planlagt.items():
        for msg, value in aendringer:
            msg.string = value
            msg.flags.discard("fuzzy")
        if aendringer:
            save(lang, cat)
            print("  %-6s %d oversættelser lagt ind" % (lang, len(aendringer)))
        lagt_ind += len(aendringer)
    return lagt_ind


def fill_danish() -> int:
    """Dansk msgstr = msgid. Kilden ER dansk, så identitet er den rigtige værdi.

    Et tomt msgstr ville falde tilbage på msgid'et og se ens ud i appen, men så
    ville "uoversat" betyde to forskellige ting alt efter sprog, og gaten kunne
    ikke være den samme for alle otte."""
    cat = load(SOURCE_LANG)
    n = 0
    for m in _entries(cat):
        msgid = m.id if isinstance(m.id, str) else m.id[0]
        if not m.string or m.fuzzy:
            m.string = msgid
            m.flags.discard("fuzzy")
            n += 1
    if n:
        save(SOURCE_LANG, cat)
    return n


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--list", metavar="FIL",
                    help="skriv de manglende oversættelser ud som JSON")
    ap.add_argument("--apply", metavar="FIL",
                    help="læg oversættelser ind fra en JSON-fil")
    ap.add_argument("--fill-danish", action="store_true",
                    help="sæt dansk msgstr = msgid hvor den er tom")
    args = ap.parse_args()

    if args.fill_danish:
        n = fill_danish()
        print("Dansk: %d poster sat til identitet." % n)
        return 0

    if args.list:
        table = missing()
        Path(args.list).write_text(
            json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
        felter = sum(len(v) for v in table.values())
        print("Skrev %d strenge (%d felter) til %s" % (len(table), felter, args.list))
        print("Udfyld hvert felt og kør: python client/check_locales.py --apply %s"
              % args.list)
        return 0

    if args.apply:
        table = json.loads(Path(args.apply).read_text(encoding="utf-8"))
        n = apply_table(table)
        if not n:
            print("\nIntet blev skrevet.")
            return 1
        print("\n%d oversættelser lagt ind. Kør nu: python client/compile_locales.py" % n)
        return 0

    problemer = check_all()
    if problemer:
        print("OVERSÆTTELSERNE ER IKKE KOMPLETTE (%d problemer)\n" % len(problemer))
        for p in problemer[:60]:
            print("  -", p)
        if len(problemer) > 60:
            print("  ... og %d mere" % (len(problemer) - 60))
        print("\nNæste skridt:")
        print("  python client/check_locales.py --list manglende.json")
        return 1
    print("Alle %d sprog er komplette: ingen fuzzy, ingen tomme, "
          "pladsholdere intakte." % len(LANGS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
