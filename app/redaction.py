"""Redaction helpers for Unite Docs (Fase 9+).

Tk-free. Turns search queries into redaction rects in **unrotated source-page
points** — the same space annotation specs live in — so the save pipeline's
redactions-first pass can apply them directly.

Empirically (verified in this codebase against rotated pages) ``page.search_for()``
already returns rects in **unrotated** source-page points — identical to
``get_text("words")`` regardless of the page's ``/Rotate`` — so no matrix transform
is applied to its results. ``Page.get_textpage`` internally resets the rotation to
0 before running the stext device, so that holds by construction, not by luck.

**Scanned pages are a different story.** They have no text layer at search time,
so ``search_for`` returns nothing at all and the user gets "Ingen forekomster" for
text that is plainly visible on screen. This module therefore falls back to
:mod:`app.ocr_text`, which rasterises the page itself, probes all four
orientations and maps the recognised words back to the same unrotated space. See
that module for why PyMuPDF's own ``get_textpage_ocr`` cannot be used directly on
a rotated page.
"""

from __future__ import annotations

import re
import unicodedata

import pymupdf

from . import ocr_text
from . import pdf_renderer
from .logging_config import get_logger

logger = get_logger(__name__)

#: Bloedt bindestreg og andre usynlige tegn OCR og PDF-tekst strør om sig med.
_INVISIBLE = dict.fromkeys(map(ord, "­​‌‍﻿"), None)


def _norm(rect) -> tuple:
    x0, x1 = sorted((rect.x0, rect.x1))
    y0, y1 = sorted((rect.y0, rect.y1))
    return (float(x0), float(y0), float(x1), float(y1))


def _fold(text: str) -> str:
    """Sammenlignelig form: uden usynlige tegn, kollapset whitespace, casefoldet."""
    text = unicodedata.normalize("NFKC", text or "").translate(_INVISIBLE)
    return re.sub(r"\s+", " ", text).casefold()


def find_text_rects(page, query: str, *, allow_ocr: bool = True,
                    rotation_hint: int = 0, cancel=None,
                    cache_path: str = "", cache_index: int = -1) -> list:
    """Rects (unrotated points) of every occurrence of ``query`` on ``page``.

    The fast ``search_for`` path comes first; on a page whose text is what is
    shown, its rects are clipped against the page's other words so a redaction
    never touches a neighbouring line. Only
    when it finds nothing *and* the page looks like a scan do we pay for OCR.
    """
    query = (query or "").strip()
    if not query:
        return []
    try:
        hits = page.search_for(query)
    except Exception as e:
        logger.debug("search_for fejlede: %s", e)
        hits = []
    if hits:
        rects = [_norm(r) for r in hits]
        # search_for's rects har skriftens fulde hoejde og rager ind i nabo-
        # linjerne paa taet sat tekst, og apply_redactions() sletter ethvert
        # tegn de roerer. Klip dem mod sidens ord -- se ocr_text.clip_to_page_words.
        # Men kun naar teksten er det synlige: over en scanning skal billedet
        # blankes i fuld hoejde.
        if not ocr_text.text_layer_visible(page):
            return rects
        try:
            words = page.get_text("words")
        except Exception as e:
            logger.debug("get_text('words') fejlede: %s", e)
            return rects
        return ocr_text.clip_to_page_words(rects, words)

    if not allow_ocr or not ocr_text.needs_ocr(page):
        return []
    words = ocr_text.page_words_a_space(
        page, rotation_hint=rotation_hint, cancel=cancel,
        cache_path=cache_path, cache_index=cache_index)
    return rects_in_words(words, query)


def rects_in_words(words, query: str) -> list:
    """Every occurrence of ``query`` in an already-extracted word list.

    Shared by the search path and the anonymizer so there is exactly one
    implementation of "find this string and give me its rectangles".
    """
    query = (query or "").strip()
    if not query or not words:
        return []
    text, offsets = ocr_text.words_to_text(words)
    hay, needle = _fold(text), _fold(query)
    if not needle:
        return []

    # ``_fold`` can change the string length (collapsed whitespace), so search in
    # the folded text but map positions back through a per-character index.
    index = _fold_index(text)
    out = []
    start = hay.find(needle)
    while start != -1:
        end = start + len(needle)
        try:
            a, b = index[start], index[end - 1] + 1
        except IndexError:
            break
        rects = ocr_text.rects_for_span(words, offsets, a, b)
        if rects:
            out.extend(rects)
        start = hay.find(needle, start + 1)
    return out


def _fold_index(text: str) -> list:
    """Map each character of ``_fold(text)`` back to its offset in ``text``."""
    normalized = unicodedata.normalize("NFKC", text or "")
    out = []
    prev_space = False
    for i, ch in enumerate(normalized):
        if ord(ch) in _INVISIBLE:
            continue
        if ch.isspace():
            if prev_space:
                continue
            prev_space = True
            out.append(i)
            continue
        prev_space = False
        # casefold() kan give flere tegn for ét (fx "ß" -> "ss").
        out.extend([i] * max(1, len(ch.casefold())))
    return out


def rects_for_words(page, sel_rect) -> list:
    """Rects (unrotated points) of the words intersecting ``sel_rect`` — the
    mouse-selection redaction path. ``sel_rect`` is already unrotated."""
    sel = pymupdf.Rect(*sel_rect)
    out = []
    for w in page.get_text("words"):
        wr = pymupdf.Rect(w[0], w[1], w[2], w[3])
        if wr.intersects(sel):
            out.append((float(w[0]), float(w[1]), float(w[2]), float(w[3])))
    return out


def words_for_page(path: str, passwords, index: int, *, rotation_hint: int = 0,
                   cancel=None) -> list:
    """Ordliste i A-space for én kildeside — OCR'et hvis siden er en scanning.

    Samme ordform som ``page.get_text("words")``, uanset hvor ordene kom fra, saa
    kalderen kan bruge dem ens. Kald fra en arbejdstraad: ``PDF_LOCK`` tages
    **pr. side** (samme grund som i :func:`scan_indices` — en OCR-koersel varer
    sekunder og maa ikke fryse miniature-optegningen).
    """
    try:
        with pdf_renderer.PDF_LOCK:
            doc = pdf_renderer._doc_cache.get_or_open(path, passwords)
            if not doc or not (0 <= index < doc.page_count):
                return []
            page = doc[index]
            if not ocr_text.needs_ocr(page):
                return list(page.get_text("words"))
            return list(ocr_text.page_words_a_space(
                page, rotation_hint=rotation_hint, cancel=cancel,
                cache_path=path, cache_index=index))
    except Exception as e:
        logger.debug("words_for_page(%s, %s) fejlede: %s", path, index, e)
        return []


def scan_indices(path: str, passwords, query: str, indices, *,
                 allow_ocr: bool = True, rotations=None, on_page=None,
                 cancel=None) -> dict:
    """Search a set of source-page indices of one file for ``query``.

    Returns ``{src_index: [rects]}`` (unrotated points), only for pages that had
    at least one hit. Call from a worker thread.

    ``PDF_LOCK`` is taken **per page**, not around the whole file: with OCR in
    play a single page can take seconds and a bundle several minutes, and holding
    the lock throughout would freeze every thumbnail render in the app.
    """
    out = {}
    if not (query or "").strip():
        return out
    rotations = rotations or {}
    for n, idx in enumerate(indices):
        if cancel is not None and cancel.is_set():
            break
        try:
            with pdf_renderer.PDF_LOCK:
                doc = pdf_renderer._doc_cache.get_or_open(path, passwords)
                if not doc or not (0 <= idx < doc.page_count):
                    continue
                rects = find_text_rects(
                    doc[idx], query, allow_ocr=allow_ocr,
                    rotation_hint=rotations.get(idx, 0), cancel=cancel,
                    cache_path=path, cache_index=idx)
        except Exception as e:
            logger.debug("scan side %s fejlede: %s", idx, e)
            rects = []
        if rects:
            out[idx] = rects
        if on_page is not None:
            on_page(n + 1)
    return out


def scan_image(path: str, query: str, *, rotation_hint: int = 0, cancel=None) -> list:
    """Search an image file (jpg/png/…) for ``query`` via OCR.

    Rects are in **PIL pixels**, which is image A-space — the same space
    ``PageEdit.crop`` uses for images.
    """
    if not (query or "").strip():
        return []
    words = ocr_text.image_words_a_space(path, rotation_hint=rotation_hint,
                                         cancel=cancel)
    return rects_in_words(words, query)
