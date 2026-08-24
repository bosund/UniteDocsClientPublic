"""Annotation materialisation for Unite Docs (Fase 6+).

Tk-free. Turns serialisable :class:`~app.edit_model.AnnotationSpec` records into
real PyMuPDF annotations on a *destination* page at save time. Geometry in specs
is stored in **unrotated source-page points** (see edit_model / plan §1.3), which
is exactly the coordinate space ``add_*_annot`` expects — so no transform is
needed here regardless of the page's ``/Rotate``.

Ordering matters (plan §1.4): **redactions first** — reset rotation, add the
redaction annots, ``apply_redactions()`` (which rewrites the content stream and
may drop overlapping annots), restore rotation — *then* the visible annots, so
visible annots are never collateral of a redaction.
"""

from __future__ import annotations

import pymupdf

from .logging_config import get_logger

logger = get_logger(__name__)

# Kind constants (mirror AnnotationSpec.kind values).
ANNOT_HIGHLIGHT = "highlight"
ANNOT_UNDERLINE = "underline"
ANNOT_STRIKEOUT = "strikeout"
ANNOT_INK = "ink"
ANNOT_LINE = "line"
ANNOT_RECT = "rect"
ANNOT_CIRCLE = "circle"
ANNOT_TEXT = "text"            # sticky note
ANNOT_FREETEXT = "freetext"
ANNOT_REDACT = "redact"

_TEXT_MARKUP = {ANNOT_HIGHLIGHT, ANNOT_UNDERLINE, ANNOT_STRIKEOUT}


def _rects(spec):
    return [pymupdf.Rect(*r) for r in spec.rects]


def apply_specs_to_page(page, specs) -> None:
    """Apply every :class:`AnnotationSpec` in ``specs`` to a live merged page."""
    redacts = [s for s in specs if s.kind == ANNOT_REDACT]
    visible = [s for s in specs if s.kind != ANNOT_REDACT]

    if redacts:
        # Redactions are computed in unrotated points; neutralise /Rotate so the
        # rects land correctly, apply, then restore the rotation.
        original_rot = page.rotation
        try:
            if original_rot:
                page.set_rotation(0)
            for s in redacts:
                fill = s.fill if s.fill is not None else (0, 0, 0)
                for r in _rects(s):
                    page.add_redact_annot(r, fill=fill)
            page.apply_redactions()
        finally:
            if original_rot:
                page.set_rotation(original_rot)

    for s in visible:
        try:
            _apply_one(page, s)
        except Exception as e:
            logger.warning("Kunne ikke anvende annotation (%s): %s", s.kind, e)


def _apply_one(page, s) -> None:
    kind = s.kind
    if kind in _TEXT_MARKUP:
        rects = _rects(s)
        if not rects:
            return
        if kind == ANNOT_HIGHLIGHT:
            annot = page.add_highlight_annot(rects)
        elif kind == ANNOT_UNDERLINE:
            annot = page.add_underline_annot(rects)
        else:
            annot = page.add_strikeout_annot(rects)
        _finish_markup(annot, s)
    elif kind == ANNOT_INK:
        polylines = [[(float(pt[0]), float(pt[1])) for pt in stroke] for stroke in s.strokes]
        polylines = [pl for pl in polylines if len(pl) >= 2]
        if not polylines:
            return
        annot = page.add_ink_annot(polylines)
        _finish_shape(annot, s)
    elif kind == ANNOT_LINE:
        if not s.rects:
            return
        x0, y0, x1, y1 = s.rects[0]
        annot = page.add_line_annot(pymupdf.Point(x0, y0), pymupdf.Point(x1, y1))
        _finish_shape(annot, s)
    elif kind == ANNOT_RECT:
        if not s.rects:
            return
        annot = page.add_rect_annot(_rects(s)[0])
        _finish_shape(annot, s)
    elif kind == ANNOT_CIRCLE:
        if not s.rects:
            return
        annot = page.add_circle_annot(_rects(s)[0])
        _finish_shape(annot, s)
    elif kind == ANNOT_TEXT:
        if not s.rects:
            return
        x0, y0, _x1, _y1 = s.rects[0]
        page.add_text_annot(pymupdf.Point(x0, y0), s.text or "")
    elif kind == ANNOT_FREETEXT:
        if not s.rects:
            return
        annot = page.add_freetext_annot(
            _rects(s)[0], s.text or "", fontsize=s.fontsize,
            text_color=s.color, fill_color=s.fill)
        try:
            annot.update()
        except Exception:
            pass


def _finish_markup(annot, s) -> None:
    """Text-markup annots (highlight/underline/strikeout) take a single stroke
    colour; opacity is honoured for a lighter highlight."""
    try:
        annot.set_colors(stroke=s.color)
        if s.opacity is not None and s.opacity < 1.0:
            annot.set_opacity(s.opacity)
        annot.update()
    except Exception as e:
        logger.debug("markup finish: %s", e)


def _finish_shape(annot, s) -> None:
    """Line/ink/rect/circle: stroke colour + optional fill + border width."""
    try:
        if s.fill is not None:
            annot.set_colors(stroke=s.color, fill=s.fill)
        else:
            annot.set_colors(stroke=s.color)
        try:
            annot.set_border(width=s.width)
        except Exception:
            pass
        if s.opacity is not None and s.opacity < 1.0:
            annot.set_opacity(s.opacity)
        annot.update()
    except Exception as e:
        logger.debug("shape finish: %s", e)
