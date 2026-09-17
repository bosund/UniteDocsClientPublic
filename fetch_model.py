"""Hent sprogmodellerne til automatisk anonymisering.

Modellerne ligger **ikke** i git: godt 100 MB binaere vaegte ville blive
liggende i historikken for altid. De hoerer til i ``client/models/<navn>/``,
hvor ``app/pii.model_dir()`` finder dem, og hvorfra compileren bundter dem som
data (``--include-data-dir=...=models``) praecis som ``tessdata``.

Koer scriptet efter en frisk klon, og foer en build:

    python client/fetch_model.py            # begge modeller
    python client/fetch_model.py --only da  # kun den ene

To modeller, to roller:

``da_core_news_md``
    Standardsproget. Koeres paa **hver** side, og de danske formatgenkendere
    (CPR, CVR, konto, adresse) haenger paa den.
``en_core_web_md``
    Laegges oven i, naar en side ser engelsk ud. Maalt paa engelske dokumenter
    fandt den danske model alene 12 af 18 navne; med den engelske 17 af 18.

Er en model allerede pip-installeret (``requirements-dev.txt``), kopieres den
derfra; ellers hentes wheel'en fra explosions officielle GitHub-udgivelse og
pakkes ud. Wheel'en er en zip-fil, saa der er ingen ekstra afhaengigheder.

Licenser: den danske models vaegte er **CC BY-SA 4.0** (share-alike), den
engelske **MIT**. Begge distribueres som datamapper ved siden af binaeren -- ikke
linket ind i den -- og deres egne ``LICENSE``-filer kopieres med. Attributionen
staar i ``app/credits.py`` og NOTICE.
"""

import argparse
import hashlib
import importlib
import io
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

#: sprognoegle -> (pakkenavn, version). Noeglerne matcher ``pii.MODEL_NAMES``.
MODELS = {
    "da": ("da_core_news_md", "3.8.0", "CC BY-SA 4.0"),
    "en": ("en_core_web_md", "3.8.0", "MIT"),
}

#: Sprogdetektoren. Facebooks lid.176 i den komprimerede form: 0,9 MB mod
#: 125 MB for ``.bin``, uden maalbart tab paa vores materiale. Licens
#: **CC BY-SA 3.0** — se ``app/credits.py``.
LID_NAME = "lid.176.ftz"
LID_URL = ("https://dl.fbaipublicfiles.com/fasttext/supervised-models/%s"
           % LID_NAME)

CLIENT_DIR = Path(__file__).resolve().parent
MODELS_DIR = CLIENT_DIR / "models"

#: Filen der beviser at en mappe faktisk er en spaCy-model.
MARKER = "config.cfg"


def _url(name: str, version: str) -> str:
    wheel = "%s-%s-py3-none-any.whl" % (name, version)
    return ("https://github.com/explosion/spacy-models/releases/download/"
            "%s-%s/%s" % (name, version, wheel))


def _installed_model_dir(name: str):
    """Mappen for en pip-installeret model, eller None."""
    try:
        mod = importlib.import_module(name)
    except ImportError:
        return None
    base = Path(mod.__file__).resolve().parent
    if (base / MARKER).exists():
        return base
    # pip-layout: en_core_web_md/en_core_web_md-3.8.0/
    for child in sorted(base.iterdir()):
        if child.is_dir() and (child / MARKER).exists():
            return child
    return None


def _extract_from_wheel(raw: bytes, name: str, version: str, dest: Path) -> None:
    """Pak modelmappen ud af wheel'en direkte til ``dest``."""
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        # Inde i wheel'en: en_core_web_md/en_core_web_md-3.8.0/...
        prefix = "%s/%s-%s/" % (name, name, version)
        names = [n for n in zf.namelist() if n.startswith(prefix)]
        if not names:
            raise RuntimeError("wheel'en har ikke det forventede layout (%s)" % prefix)
        for member in names:
            if member.endswith("/"):
                continue
            rel = member[len(prefix):]
            target = (dest / rel).resolve()
            # Zip-slip-vaern: et medlemsnavn maa ikke pege uden for dest.
            if not str(target).startswith(str(dest.resolve())):
                raise RuntimeError("wheel'en indeholder en sti uden for maalet: %s"
                                   % member)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(member) as src, io.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)


def _download(url: str) -> bytes:
    print("  henter %s ..." % url)
    with urllib.request.urlopen(url, timeout=180) as resp:
        raw = resp.read()
    print("  %.1f MB hentet (sha256 %s)"
          % (len(raw) / 1e6, hashlib.sha256(raw).hexdigest()[:16]))
    return raw


def _size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1e6


def fetch(lang: str, force: bool = False) -> int:
    name, version, license_ = MODELS[lang]
    dest = MODELS_DIR / name
    print("[%s] %s %s" % (lang, name, version))

    if (dest / MARKER).exists() and not force:
        print("  ligger allerede i %s (%.0f MB) — brug --force for at hente igen."
              % (dest, _size_mb(dest)))
        return 0

    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)

    src = _installed_model_dir(name)
    if src is not None:
        print("  kopierer fra den pip-installerede model: %s" % src)
        shutil.copytree(src, dest)
    else:
        try:
            _extract_from_wheel(_download(_url(name, version)), name, version, dest)
        except Exception as exc:
            print("  FEJL: kunne ikke hente modellen: %s" % exc, file=sys.stderr)
            print("  Alternativ: pip install %s" % _url(name, version),
                  file=sys.stderr)
            return 1

    if not (dest / MARKER).exists():
        print("  FEJL: %s mangler efter udpakning." % MARKER, file=sys.stderr)
        return 1

    print("  klar: %s (%.0f MB), licens %s" % (dest, _size_mb(dest), license_))
    return 0


def fetch_lid(force: bool = False) -> int:
    """Hent sprogdetektoren. Én fil, ingen udpakning."""
    dest = MODELS_DIR / LID_NAME
    print("[lid] %s" % LID_NAME)
    if dest.exists() and not force:
        print("  ligger allerede i %s (%.1f MB)."
              % (dest, dest.stat().st_size / 1e6))
        return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = _download(LID_URL)
    except Exception as exc:
        print("  FEJL: kunne ikke hente sprogdetektoren: %s" % exc, file=sys.stderr)
        print("  Uden den falder detektionen tilbage paa ordlister, som misser "
              "engelsk uden funktionsord.", file=sys.stderr)
        return 1
    dest.write_bytes(raw)
    print("  klar: %s (%.1f MB), licens CC BY-SA 3.0" % (dest, len(raw) / 1e6))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--force", action="store_true",
                    help="hent igen selvom modellen allerede ligger der")
    ap.add_argument("--only", choices=sorted(MODELS) + ["lid"],
                    help="hent kun ét (standard: alle)")
    args = ap.parse_args()

    if args.only == "lid":
        return fetch_lid(args.force)
    langs = [args.only] if args.only else sorted(MODELS)
    rc = 0
    for lang in langs:
        rc |= fetch(lang, args.force)
    if args.only is None:
        rc |= fetch_lid(args.force)
    return rc


if __name__ == "__main__":
    sys.exit(main())
