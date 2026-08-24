"""Redaction helpers for Unite Docs (Fase 9+).

Tk-free. Turns search queries (and, later, Presidio spans) into redaction rects in
**unrotated source-page points**, the same space annotation specs live in, so the
save pipeline's redactions-first pass can apply them directly.

Empirically (PyMuPDF 1.28.2, verified against rotated pages) ``page.search_for()``
already returns rects in **unrotated** source-page points — identical to
``get_text("words")`` regardless of the page's ``/Rotate`` — so no matrix transform
is applied here. (This was checked directly rather than trusted from memory.)
"""

from __future__ import annotations

import pymupdf

from . import pdf_renderer
from .logging_config import get_logger

logger = get_logger(__name__)


def _norm(rect) -> tuple:
    x0, x1 = sorted((rect.x0, rect.x1))
    y0, y1 = sorted((rect.y0, rect.y1))
    return (float(x0), float(y0), float(x1), float(y1))


def find_text_rects(page, query: str) -> list:
    """Rects (unrotated points) of every occurrence of ``query`` on ``page``."""
    query = (query or "").strip()
    if not query:
        return []
    try:
        hits = page.search_for(query)
    except Exception as e:
        logger.debug("search_for fejlede: %s", e)
        return []
    return [_norm(r) for r in hits]


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


def scan_indices(path: str, passwords, query: str, indices) -> dict:
    """Search a set of source-page indices of one file for ``query``.

    Returns ``{src_index: [rects]}`` (unrotated points), only for pages that had
    at least one hit. Opens the document once under PDF_LOCK via the shared cache;
    call from a worker thread — a whole-document scan can be slow."""
    out = {}
    if not (query or "").strip():
        return out
    with pdf_renderer.PDF_LOCK:
        doc = pdf_renderer._doc_cache.get_or_open(path, passwords)
        if not doc:
            return out
        for idx in indices:
            if not (0 <= idx < doc.page_count):
                continue
            try:
                rects = find_text_rects(doc[idx], query)
            except Exception as e:
                logger.debug("scan side %s fejlede: %s", idx, e)
                rects = []
            if rects:
                out[idx] = rects
    return out
