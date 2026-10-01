"""Autoupdater - henter og installerer nye versioner fra www.uniteapps.dk.

Modulet er bevidst **fri for Tkinter**: al UI ligger i ``main_app.py``, saa
logikken her kan enhedstestes uden et vindue. Den eneste netvaerksklient er
stdlib'ens ``urllib.request`` - en ny afhaengighed ville kraeve en post i
``credits.py`` (CLAUDE.md regel 4) og mere Nuitka-bagage, uden at give noget.

Serverkontrakten (to offentlige endpoints, ingen login):

``GET /api/unitedocs/version?current=<version>&install=<uuid>``
    ``install`` er et tilfaeldigt, anonymt installations-id (se
    :func:`get_install_id`), som serveren bruger som besoegs-id i Umami, saa
    maskiner bag samme NAT ikke taeller som een. Serveren ignorerer et
    ugyldigt id og svarer det samme med og uden. JSON med ``version``, ``file_name``, ``size``, ``sha256``, ``release_notes``,
    ``download_url``, ``info_url``. Ligger der ingen installer, svares ``404``
    med ``{"available": false}`` - det er et normalt svar, ikke en fejl.

``GET /api/unitedocs/download``
    Fast URL. ``Accept-Ranges: bytes`` (afbrudt download kan genoptages),
    ``X-UniteDocs-SHA256`` i headeren, ``ETag`` = samme hash.

To ting er ikke til forhandling:

1. **Hash-kontrollen.** Vi koerer en ``.exe`` bagefter; uden en verificeret
   SHA-256 er en halv eller ombyttet download ikke til at skelne fra en god.
   Mangler serveren en hash, installerer vi ikke - saa gaar turen til
   hjemmesiden i stedet.
2. **Kun vores egen host.** ``download_url`` kommer fra et svar udefra, saa den
   valideres mod :data:`_TRUSTED_HOSTS`, foer den bruges.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import utils

logger = logging.getLogger(__name__)

VERSION_URL = "https://www.uniteapps.dk/api/unitedocs/version"
DOWNLOAD_URL = "https://www.uniteapps.dk/api/unitedocs/download"
INFO_URL = "https://www.uniteapps.dk/unitedocs"

#: Der gaar en uge mellem to automatiske tjek.
CHECK_INTERVAL_S = 7 * 24 * 3600

#: Undermappe under ``utils.get_app_data_path()`` hvor installere hentes ned.
DOWNLOAD_SUBDIR = "updates"

_TRUSTED_HOSTS = ("uniteapps.dk", "www.uniteapps.dk")

_NET_TIMEOUT = 15
_DOWNLOAD_TIMEOUT = 60
_CHUNK = 64 * 1024
_PROGRESS_STEP = 256 * 1024
_MAX_JSON_BYTES = 256 * 1024
_MAX_INSTALLER_BYTES = 512 * 1024 * 1024

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]")
_LEADING_DIGITS = re.compile(r"\d+")

_HELPER_NAME = "_finish_update.cmd"
_INSTALLER_ARGS = (
    "/SILENT",
    "/SUPPRESSMSGBOXES",
    "/NORESTART",
    "/CLOSEAPPLICATIONS",
    "/FORCECLOSEAPPLICATIONS",
)


class UpdateError(Exception):
    """Opdateringen kunne ikke gennemfoeres."""


class UpdateCancelled(UpdateError):
    """Brugeren afbroed download'en."""


class ChecksumError(UpdateError):
    """Den hentede fil matcher ikke serverens SHA-256."""


# ---------------------------------------------------------------------------
# Versionssammenligning
# ---------------------------------------------------------------------------

def parse_version(text) -> tuple:
    """``"8.0.10"`` -> ``(8, 0, 10)``.

    Mere tilgivende end ``__version__.py``'s egen udledning: et led som
    ``"0rc1"`` bliver til ``0`` i stedet for at faa hele tuplen til at vaere
    tavst forkert. Kaster aldrig - ubrugelig input giver ``()``.
    """
    if not text:
        return ()
    parts = []
    for chunk in str(text).strip().split("."):
        m = _LEADING_DIGITS.match(chunk.strip())
        if not m:
            break
        parts.append(int(m.group()))
    return tuple(parts)


def is_newer(remote, local) -> bool:
    """Er ``remote`` en nyere version end ``local``?

    Nulpolstrer den korteste, saa ``8.0`` og ``8.0.0`` er samme version og
    ``8.0.10`` er nyere end ``8.0.9`` (en ren strengsammenligning ville sige
    det modsatte).
    """
    r = parse_version(remote)
    if not r:
        return False
    l = parse_version(local)
    n = max(len(r), len(l))
    return r + (0,) * (n - len(r)) > l + (0,) * (n - len(l))


# ---------------------------------------------------------------------------
# Svaret fra serveren
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    version: str
    file_name: str = ""
    size: int = 0
    sha256: str = ""
    release_notes: str = ""
    download_url: str = DOWNLOAD_URL
    info_url: str = INFO_URL
    published_at: str = ""
    update_available: bool = False


def _user_agent(current_version) -> str:
    return "UniteDocs/%s (Windows)" % (current_version or "0.0.0")


def _ssl_context() -> ssl.SSLContext:
    # Henter CA-certifikaterne fra Windows' eget certifikatlager, saa der ikke
    # skal bundtes en certifi-kopi med builden.
    return ssl.create_default_context()


def _is_trusted_url(url) -> bool:
    try:
        parts = urllib.parse.urlsplit(str(url))
    except ValueError:
        return False
    return parts.scheme == "https" and parts.hostname in _TRUSTED_HOSTS


def _as_int(value, fallback=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


# ---------------------------------------------------------------------------
# Anonymt installations-id
# ---------------------------------------------------------------------------

_INSTALL_ID_SECTION = "Updates"
_INSTALL_ID_OPTION = "install_id"


def _valid_install_id(value):
    """Kanonisk UUID-streng, eller ``None`` hvis ``value`` ikke er et UUID."""
    text = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(text)
    except (ValueError, TypeError, AttributeError):
        return None
    # uuid.UUID accepterer ogsaa {...}, urn: og uden bindestreger. Vi gemmer
    # kun den kanoniske form, saa alt andet regnes for ugyldigt og erstattes.
    return str(parsed) if str(parsed) == text else None


def get_install_id(config):
    """Hent installations-id'et fra ``[Updates] install_id`` - opret det ved behov.

    Id'et er ``uuid4()`` og intet andet: det maa ikke afledes af maskinnavn,
    brugernavn, MAC-adresse, SID eller noget andet der kan pege paa en person.
    ``%APPDATA%`` er roaming, saa paa domaene-pc'er foelger id'et brugeren
    mellem maskiner. Det er accepteret - vi taeller brugere snarere end maskiner.

    Kaster aldrig. Kan config ikke laeses eller skrives, returneres ``None``,
    og tjekket sendes saa uden ``install``.
    """
    try:
        existing = _valid_install_id(
            config.get(_INSTALL_ID_SECTION, _INSTALL_ID_OPTION, fallback=""))
    except Exception as exc:
        logger.debug("Kunne ikke laese install_id: %s", exc)
        return None
    if existing:
        return existing

    new_id = str(uuid.uuid4())
    try:
        old = config.get(_INSTALL_ID_SECTION, _INSTALL_ID_OPTION, fallback="")
        config.set(_INSTALL_ID_SECTION, _INSTALL_ID_OPTION, new_id)
        try:
            config.save()
        except Exception:
            # Et id der kun lever i hukommelsen, ville blive et nyt ved naeste
            # opstart og taelle samme installation to gange. Saa hellere intet.
            config.set(_INSTALL_ID_SECTION, _INSTALL_ID_OPTION, old or "")
            raise
    except Exception as exc:
        logger.debug("Kunne ikke gemme install_id: %s", exc)
        return None
    return new_id


def check(current_version, timeout=_NET_TIMEOUT, url=VERSION_URL,
          install_id=None):
    """Spoerg serveren om nyeste version.

    Returnerer ``None`` naar der intet er at hente (``404`` /
    ``available: false`` / svar uden versionsnummer), ellers en
    :class:`UpdateInfo` hvor ``update_available`` er **vores egen**
    sammenligning. Serverens felt af samme navn er altid ``true``, naar
    ``?current=`` mangler eller ikke kunne laeses, saa det bruges ikke som
    facit.

    ``install_id`` sendes med som ``install``, naar det er et gyldigt UUID;
    ellers sendes tjekket uden - id'et maa aldrig faa kaldet til at fejle.

    Kaster :class:`UpdateError` ved netvaerks- eller formatfejl.
    """
    params = {"current": current_version or ""}
    install = _valid_install_id(install_id)
    if install:
        params["install"] = install
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        "%s?%s" % (url, query),
        headers={"User-Agent": _user_agent(current_version),
                 "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as resp:
            raw = resp.read(_MAX_JSON_BYTES)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.info("Ingen installer tilgaengelig paa serveren (404).")
            return None
        raise UpdateError("Serveren svarede %s" % exc.code) from exc
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        raise UpdateError(str(exc)) from exc

    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, AttributeError) as exc:
        raise UpdateError("Ulaeseligt svar fra serveren") from exc
    if not isinstance(data, dict):
        raise UpdateError("Ulaeseligt svar fra serveren")

    if data.get("available") is False:
        return None
    version = str(data.get("version") or "").strip()
    if not version:
        return None
    if data.get("current_version_error"):
        logger.warning("Serveren kunne ikke laese vores versionsnummer: %s",
                       data.get("current_version_error"))

    download_url = str(data.get("download_url") or "").strip()
    if not _is_trusted_url(download_url):
        if download_url:
            logger.warning("Ignorerer utrovaerdig download_url: %s", download_url)
        download_url = DOWNLOAD_URL
    info_url = str(data.get("info_url") or "").strip()
    if not _is_trusted_url(info_url):
        info_url = INFO_URL

    return UpdateInfo(
        version=version,
        file_name=str(data.get("file_name") or ""),
        size=_as_int(data.get("size")),
        sha256=str(data.get("sha256") or "").strip().lower(),
        release_notes=str(data.get("release_notes") or "").strip(),
        download_url=download_url,
        info_url=info_url,
        published_at=str(data.get("published_at") or ""),
        update_available=is_newer(version, current_version),
    )


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

def _safe_file_name(name, version) -> str:
    """Goer serverens filnavn til et harmloest basename.

    Navnet kommer udefra og bruges som sti, saa baade mappeadskillere og
    ``..`` skal vaere umulige. Alt uden for ``[A-Za-z0-9._-]`` bliver til
    ``_``, hvilket ogsaa rammer begge slags skraastreger; er resultatet saa
    ikke et paent ``.exe``-navn, bygges navnet i stedet ud fra versionen.
    """
    base = _UNSAFE_NAME.sub("_", str(name or "").strip())
    if base and not base.startswith(".") and base.lower().endswith(".exe"):
        return base
    return "unitedocs-v%s-setup.exe" % _UNSAFE_NAME.sub("_", str(version or "new"))


def _hash_file(path, digest) -> int:
    read = 0
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            read += len(chunk)
    return read


def download(info, dest_dir, progress_cb=None, cancel_event=None,
             timeout=_DOWNLOAD_TIMEOUT) -> Path:
    """Hent installeren, verificer SHA-256 og returner stien til filen.

    Der skrives til ``<navn>.exe.part``; filen faar foerst sit rigtige navn,
    naar hashen passer. En efterladt ``.part`` genoptages med en
    Range-request, saa et afbrudt forsoeg ikke koster de 25 MB igen.
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_file_name(info.file_name, info.version)
    final = dest_dir / name
    part = dest_dir / (name + ".part")

    expected = (info.sha256 or "").strip().lower()
    url = info.download_url if _is_trusted_url(info.download_url) else DOWNLOAD_URL

    # Er .part allerede komplet fra sidste forsoeg, er der intet at hente.
    if expected and info.size and part.exists() and part.stat().st_size == info.size:
        d = hashlib.sha256()
        _hash_file(part, d)
        if d.hexdigest() == expected:
            final.unlink(missing_ok=True)
            part.replace(final)
            if progress_cb:
                progress_cb(info.size, info.size)
            return final
        part.unlink(missing_ok=True)

    digest = hashlib.sha256()
    done = 0
    headers = {"User-Agent": _user_agent(info.version),
               "Accept": "application/octet-stream"}
    if expected and part.exists():
        size_on_disk = part.stat().st_size
        if info.size and size_on_disk >= info.size:
            part.unlink(missing_ok=True)      # ellers svarer serveren 416
        elif size_on_disk > 0:
            done = size_on_disk
            headers["Range"] = "bytes=%d-" % done

    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as resp:
            status = getattr(resp, "status", None) or resp.getcode()
            content_len = _as_int(resp.headers.get("Content-Length"), 0)
            header_hash = (resp.headers.get("X-UniteDocs-SHA256") or "").strip().lower()
            if not expected:
                expected = header_hash

            if status == 206 and done:
                # Serveren accepterede Range: de bytes vi allerede har, skal
                # med i hashen, foer resten skrives ovenpaa.
                _hash_file(part, digest)
                mode = "ab"
                total = done + content_len if content_len else info.size
            else:
                # 200 = Range ignoreret (eller intet at genoptage) -> forfra.
                done = 0
                digest = hashlib.sha256()
                mode = "wb"
                total = content_len or info.size

            last_report = -1
            with open(part, mode) as out:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        raise UpdateCancelled("Download afbrudt")
                    chunk = resp.read(_CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if done > _MAX_INSTALLER_BYTES:
                        raise UpdateError("Filen er urimeligt stor")
                    if progress_cb and done - last_report >= _PROGRESS_STEP:
                        last_report = done
                        progress_cb(done, total)
            if progress_cb:
                progress_cb(done, total or done)
    except UpdateCancelled:
        part.unlink(missing_ok=True)
        raise
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise UpdateError("Serveren svarede %s" % exc.code) from exc
    except (urllib.error.URLError, ssl.SSLError, OSError) as exc:
        raise UpdateError(str(exc)) from exc

    if not expected:
        # Uden en hash kan vi ikke skelne en god download fra en halv, og det
        # er en .exe der skal koeres bagefter. Saa hellere ingen installation.
        part.unlink(missing_ok=True)
        raise ChecksumError("Serveren oplyste ingen kontrolsum")
    if digest.hexdigest() != expected:
        part.unlink(missing_ok=True)
        raise ChecksumError("Den hentede fil matcher ikke serverens kontrolsum")

    final.unlink(missing_ok=True)
    part.replace(final)
    logger.info("Opdatering hentet: %s (%d bytes)", final, done)
    return final


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------

def can_install() -> bool:
    """Kan vi overhovedet koere en installer over os selv?

    Kun i en frossen build. Koerer appen fra kildekode, ville installeren
    skrive til en helt anden mappe end den, der er i gang - saa peger vi
    brugeren mod hjemmesiden i stedet.

    Spoerg **altid** gennem :func:`utils.is_frozen`. Nuitka saetter ikke
    ``sys.frozen``, saa et direkte ``getattr(sys, "frozen", False)`` er falsk
    i den frosne build - og saa naegtede autoupdateren at installere netop
    dér hvor den skulle.
    """
    if os.name != "nt":
        return False
    return utils.is_frozen()


def _quote(path) -> str:
    return '"%s"' % str(path)


def launch_installer(setup_path, app_exe=None, helper_dir=None) -> bool:
    """Start installeren stille og genstart appen bagefter.

    Der spawnes en lille selvslettende ``.cmd``, fordi to ting skal ske
    *efter* at UniteDocs er lukket: installeren skal kunne skrive over
    ``unitedocs.exe`` uden fillaas, og appen skal startes igen. Inno's egen
    ``[Run]``-post er markeret ``skipifsilent`` og starter os altsaa netop
    **ikke** ved en stille installation.

    De tre sekunders ventetid og ``/CLOSEAPPLICATIONS`` er to uafhaengige
    garantier for det samme: at exe'en er sluppet, naar Inno naar dertil.
    """
    setup_path = Path(setup_path)
    if not setup_path.exists():
        raise UpdateError("Installeren blev ikke fundet: %s" % setup_path)
    app_exe = Path(app_exe) if app_exe else Path(sys.executable)
    helper_dir = Path(helper_dir) if helper_dir else setup_path.parent
    helper = helper_dir / _HELPER_NAME

    script = "\r\n".join([
        "@echo off",
        "timeout /t 3 /nobreak >nul",
        "%s %s" % (_quote(setup_path), " ".join(_INSTALLER_ARGS)),
        'start "" %s' % _quote(app_exe),
        'del "%~f0"',
        "",
    ])
    try:
        helper.write_text(script, encoding="ascii")
    except (OSError, UnicodeEncodeError) as exc:
        # Ikke-ASCII i stien ville alligevel knaekke i cmd's kodeside.
        raise UpdateError("Kunne ikke skrive opdateringsscriptet: %s" % exc) from exc

    flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
             | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
             | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        subprocess.Popen(["cmd", "/c", str(helper)],          # noqa: S603
                         cwd=str(helper_dir), creationflags=flags,
                         close_fds=True)
    except OSError as exc:
        raise UpdateError("Kunne ikke starte installeren: %s" % exc) from exc
    logger.info("Installer startet i baggrunden: %s", setup_path)
    return True


# ---------------------------------------------------------------------------
# Oprydning
# ---------------------------------------------------------------------------

def purge_old_downloads(dest_dir, keep=None, max_age_s=CHECK_INTERVAL_S) -> None:
    """Ryd gamle installere og halve downloads.

    ``utils.purge_stale_temp_files`` roerer kun ``split_cache_*`` og ``*.tmp``
    i systemets temp-mappe, saa denne mappe skal rydde op efter sig selv -
    ellers samler der sig 25 MB pr. opdatering i ``%APPDATA%``.
    """
    dest_dir = Path(dest_dir)
    if not dest_dir.is_dir():
        return
    keep_name = Path(keep).name if keep else None
    now = time.time()
    for entry in dest_dir.iterdir():
        if not entry.is_file() or entry.name == keep_name:
            continue
        try:
            if entry.name != _HELPER_NAME and now - entry.stat().st_mtime < max_age_s:
                continue
            entry.unlink()
        except OSError as exc:
            logger.debug("Kunne ikke slette %s: %s", entry, exc)
