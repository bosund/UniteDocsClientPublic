"""Save/merge pipeline for Unite Docs (Fase 2).

Tk-free so it is unit-testable headless. It consumes a snapshot of the
:class:`~app.edit_model.EditModel` (built on the main thread) and materialises the
composition into one or more output documents.

The core is a **two-pass** build (:func:`build_document`):

* **Pass 1 — insertion only.** Insert each file's pages into the merged document
  and record where each landed. Contiguous source-page runs use the fast
  ``insert_pdf(from_page, to_page)`` path; deletions/reorders degrade to one page
  at a time. ``insert_pdf`` appends, so the target indices from pass 1 stay valid.
* **Pass 2 — edits on destination pages.** Apply per-page rotation and (later)
  annotations to the *merged* pages, never to a live source page object — PyMuPDF
  invalidates source page objects across insertion.

Why the model can carry two shapes of rotation: while only the file view exists a
file's pages aren't populated, so :class:`FileJob` carries a whole-file rotation
(``pages=None``); once the page view populates/edits pages, the job carries
per-page :class:`PageJob` records instead. Both converge in pass 2.
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import pdf_utils
from . import utils
from . import export_formats
from . import edit_model as em
from .logging_config import get_logger

logger = get_logger(__name__)

_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'}


@dataclass
class PageJob:
    """One page to emit when the file's pages have been populated/edited."""
    src_index: int
    rotation: int = 0
    annots: tuple = ()
    crop: tuple | None = None         # uroterede kildeenheder, y-ned (PageEdit.crop)


@dataclass
class FileJob:
    """A Tk-free snapshot of one FileEntry for the worker thread."""
    path: str
    kind: str                         # em.KIND_PDF | em.KIND_IMAGE
    enc_key: str = ""
    rotation: int = 0                 # whole-file rotation (used when pages is None)
    crop: tuple | None = None         # images only
    pages: list | None = None         # list[PageJob] when populated, else None
    title: str = ""                   # source file name (chapter title)


def build_jobs(entries, rotations: dict | None = None,
               croppings: dict | None = None) -> list:
    """Main-thread snapshot: turn a list of FileEntry (+ the per-file rotation/crop
    dicts still used by the file view) into a list of Tk-free FileJobs, in order.

    Pass ``model.files`` to merge everything, or a filtered subset (e.g. only the
    decryptable files) for save-each. When a file's pages are populated (page view
    has touched it) each page's own rotation/annots are used and the per-file dicts
    are ignored; otherwise the whole-file rotation/crop from the dicts apply."""
    rotations = rotations or {}
    croppings = croppings or {}
    jobs = []
    for f in entries:
        title = Path(f.path).name
        if f.pages_loaded and f.pages:
            # En fils sider kan pege på FLERE kildefiler: "Indsæt side" lægger
            # genererede sider ind midt i listen med deres egen src_path. Del
            # derfor op i sammenhængende løb pr. src_path — ét FileJob pr. løb,
            # så rækkefølgen bevares og hvert job åbner den rigtige fil.
            for src_path, run in _src_runs(f.pages):
                own = os.path.normcase(src_path) == os.path.normcase(f.path)
                pages = [PageJob(src_index=p.src_index, rotation=p.rotation,
                                 annots=p.annots, crop=p.crop)
                         for p in run]
                jobs.append(FileJob(
                    path=src_path,
                    kind=f.kind if own else em.kind_for_path(src_path),
                    # Genererede sider er aldrig krypterede.
                    enc_key=f.enc_key if own else pdf_utils.ENC_NOT_ENCRYPTED,
                    pages=pages,
                    title=title if own else Path(src_path).name))
        else:
            jobs.append(FileJob(
                path=f.path, kind=f.kind, enc_key=f.enc_key,
                rotation=rotations.get(f.iid, 0), crop=croppings.get(f.iid),
                pages=None, title=title))
    return jobs


def _src_runs(pages: list):
    """Gruppér en fils PageEdits i maksimale løb der deler ``src_path``.

    Yields ``(src_path, [PageEdit, ...])`` i listens rækkefølge."""
    run = []
    cur = None
    for p in pages:
        if run and os.path.normcase(p.src_path) == os.path.normcase(cur):
            run.append(p)
            continue
        if run:
            yield cur, run
        cur, run = p.src_path, [p]
    if run:
        yield cur, run


def _runs(pages: list):
    """Group a page list into maximal runs of consecutive src_index (+1 step)."""
    run = []
    for pj in pages:
        if run and pj.src_index == run[-1].src_index + 1:
            run.append(pj)
        else:
            if run:
                yield run
            run = [pj]
    if run:
        yield run


def _cropbox_from_view_rect(page, crop):
    """``PageEdit.crop`` (uroterede kildeenheder, y-NED, origo = ``page.rect``s
    oeverste venstre hjoerne) -> rektanglet ``page.set_cropbox()`` vil have.

    Maalt empirisk mod PyMuPDF, ikke udledt af dokumentationen (samme disciplin
    som ``view_transform``). Hvad maalingen viste:

    * ``set_cropbox`` bruger **samme y-NED-rum som ``page.rect``/``page.mediabox``**
      -- der skal **ingen** y-vending til. En y-vendt formel beskaerer den modsatte
      ende af siden.
    * Den er **uafhaengig af ``/Rotate``**: samme rektangel giver det rigtige
      papiromraade ved 0/90/180/270, og ``page.rect`` faar automatisk byttet om
      paa bredde/hoejde.
    * Raekkefoelgen ``set_rotation``/``set_cropbox`` er ligegyldig.
    * Rektanglet er **relativt til den eksisterende cropbox**, ikke til mediaboxen:
      ``page.rect`` ER cropboxen. Dens oeverste venstre hjoerne skal derfor laegges
      til. Uden det fejler en side med forskudt mediabox haardt ("CropBox not in
      MediaBox"), og en side der allerede har en cropbox bliver beskaaret i
      forhold til mediaboxen i stedet for i forhold til det brugeren ser.
    * Brug ``page.cropbox.x0/.y0`` -- **ikke** ``page.cropbox_position``. Sidstnaevnte
      blander konventioner: for en mediabox (10,10,605,852) uden egen cropbox
      rapporterer den (10, 0), fordi x er raa PDF-x0 mens y maales fra toppen.
      Det gav en beskaering der var 10 pt for lav.

    Returnerer None hvis resultatet er tomt/ugyldigt, saa kaldestedet kan lade
    siden vaere ubeskaaret frem for at kaste.
    """
    try:
        cb = page.cropbox
        # Klem FOERST i A-rum mod sidens egen synlige boks. Man maa IKKE snitte
        # resultatet med page.mediabox bagefter: PyMuPDF rapporterer de to bokse i
        # hver sit y-frame (mediabox (10,10,605,852) vs cropbox (10,0,605,842) for
        # samme side), saa et snit ville barbere forskellen af beskaeringen.
        # cb.width/cb.height er uroterede og dermed lige praecis A-rummets maal.
        x0 = min(max(0.0, crop[0]), cb.width)
        y0 = min(max(0.0, crop[1]), cb.height)
        x1 = min(max(0.0, crop[2]), cb.width)
        y1 = min(max(0.0, crop[3]), cb.height)
        r = pymupdf.Rect(x0 + cb.x0, y0 + cb.y0, x1 + cb.x0, y1 + cb.y0).normalize()
        if r.is_empty or not r.is_valid or r.width < 1 or r.height < 1:
            return None
        return r
    except Exception as e:
        logger.warning("Kunne ikke omregne beskaering %s: %s", crop, e)
        return None


def _append_error_page(merged, fname, is_pdf, chapters):
    """PDF: insert a rendered error page. Text: register an error chapter instead
    (a rendered error page would be extracted as stray prose)."""
    if is_pdf:
        with pymupdf.open(stream=utils.create_error_pdf(fname).read(), filetype="pdf") as err_src:
            merged.insert_pdf(err_src)
    else:
        chapters.append(export_formats.Chapter(
            title=fname, error_text=_("[Filen %s kunne ikke åbnes.]") % fname))


def build_document(jobs, pw_list, *, is_pdf, apply_annots=None,
                   report=None, cancel=None):
    """Two-pass build of one merged document from ``jobs``.

    Returns ``(merged_doc, ok, fail, chapters)``. Caller owns closing ``merged_doc``.
    ``apply_annots(page, annots)`` is an optional hook (Fase 6) — annotations are
    ignored when it is None. ``report(i, total)`` is an optional progress callback.
    """
    merged = pymupdf.open()
    ok = fail = 0
    chapters = []
    applied = []          # [(target_index, rotation, annots, crop)]
    total = len(jobs)

    # --- PASS 1: insertion --------------------------------------------------
    for i, job in enumerate(jobs):
        if cancel is not None and cancel():
            raise export_formats.ExportCancelled()
        if report:
            report(i + 1, total)
        try:
            if job.kind == em.KIND_IMAGE:
                # Image: rotation/crop are baked into the A4 raster. A populated
                # image page carries its rotation on pages[0] instead.
                rot = job.pages[0].rotation if job.pages else job.rotation
                # Billeder: beskaeringen er i PIL-pixels og bages ind i A4-rasteren.
                crop = job.pages[0].crop if job.pages else job.crop
                buf = pdf_utils._image_to_pdf_a4(job.path, rot, crop)
                if not buf:
                    fail += 1
                    _append_error_page(merged, job.title, is_pdf, chapters)
                    continue
                start = merged.page_count
                with pymupdf.open(stream=buf.read(), filetype="pdf") as src:
                    merged.insert_pdf(src)
                if not is_pdf:
                    chapters.append(export_formats.Chapter(
                        title=job.title, first_page=start, last_page=merged.page_count))
                ok += 1
                continue

            src = pdf_utils.open_with_passwords(job.path, pw_list)
            if not src:
                fail += 1
                _append_error_page(merged, job.title, is_pdf, chapters)
                continue
            try:
                start = merged.page_count
                if job.pages is None:
                    # Whole file (file view); per-file rotation applied in pass 2.
                    merged.insert_pdf(src)
                    for k in range(start, merged.page_count):
                        applied.append((k, job.rotation, (), None))
                else:
                    # Populated pages: insert as consecutive runs (fast path),
                    # then record each landed page with its own edit.
                    for run in _runs(job.pages):
                        merged.insert_pdf(src, from_page=run[0].src_index,
                                          to_page=run[-1].src_index)
                    for k, pj in enumerate(job.pages):
                        applied.append((start + k, pj.rotation, pj.annots, pj.crop))
                if not is_pdf:
                    chapters.append(export_formats.Chapter(
                        title=job.title, first_page=start, last_page=merged.page_count))
                ok += 1
            finally:
                src.close()
        except Exception as e:
            logger.error("Fejl ved behandling af fil %s: %s", job.title, e)
            fail += 1
            _append_error_page(merged, job.title, is_pdf, chapters)

    # --- PASS 2: edits on destination pages --------------------------------
    # Raekkefoelgen er bevidst: annotationer -> rotation -> cropbox TIL SIDST.
    # apply_specs_to_page saetter midlertidigt set_rotation(0) og koerer
    # apply_redactions(); begge regner i det origo page.rect havde da geometrien
    # blev laest. Saettes cropboxen foerst, forskydes det origo (cropbox_position)
    # og hver eneste annotation og maskering ville lande forkert.
    for target_index, rotation, annots, crop in applied:
        page = merged[target_index]
        if annots and apply_annots is not None:
            apply_annots(page, annots)          # redactions-first handled inside the hook
        if rotation:
            page.set_rotation((page.rotation + rotation) % 360)
        if crop:
            box = _cropbox_from_view_rect(page, crop)
            if box is not None:
                page.set_cropbox(box)

    return merged, ok, fail, chapters


def merge_worker(out_path, jobs, pw_list, fmt, *, apply_annots=None, report=None,
                 ocr_report=None, export_report=None, export_progress=None,
                 cancel=None):
    """Merge all jobs into one document and save it as ``fmt``.

    Returns ``dict(ok, fail, missing_pages)``. Raises ``export_formats.ExportError``
    (or other exceptions) on save/export failure — caller handles UI."""
    is_pdf = fmt == export_formats.FORMAT_PDF
    merged, ok, fail, chapters = build_document(
        jobs, pw_list, is_pdf=is_pdf, apply_annots=apply_annots, report=report,
        cancel=cancel)
    try:
        if is_pdf:
            if export_formats.ocr_available():
                if ocr_report:
                    ocr_report()
                export_formats.add_searchable_ocr_layer(merged)
            merged.save(out_path, garbage=3, deflate=True)
            return {"ok": ok, "fail": fail, "missing_pages": 0}
        else:
            if export_report:
                export_report(export_formats.extension_for(fmt))
            res = export_formats.write_document(
                fmt, doc=merged, out_path=out_path, chapters=chapters, title="",
                progress=export_progress, cancel=cancel)
            return {"ok": ok, "fail": fail, "missing_pages": res.missing_pages}
    finally:
        merged.close()


def save_each_worker(jobs, pw_list, fmt, dest_folder, *, apply_annots=None,
                     progress_report=None, ocr_report=None, export_report=None,
                     export_progress=None, cancel=None):
    """Save each job to its own output file, honouring rotation/edits (the old
    ``_save_dec_worker`` ignored rotation entirely — this fixes that).

    Returns ``dict(saved, failed, missing_pages)``."""
    saved = failed = missing_pages = 0
    is_pdf = fmt == export_formats.FORMAT_PDF
    extension = export_formats.extension_for(fmt)
    total = len(jobs)
    source_paths = {os.path.normcase(os.path.abspath(j.path)) for j in jobs}

    for i, job in enumerate(jobs):
        if progress_report:
            progress_report(i + 1, total)
        op = Path(job.path)
        # Build a one-file edited document via the shared two-pass helper.
        if cancel is not None and cancel():
            raise export_formats.ExportCancelled()
        doc, ok, fail, chapters = build_document(
            [job], pw_list, is_pdf=is_pdf, apply_annots=apply_annots)
        try:
            if ok == 0:
                failed += 1
                continue
            if is_pdf:
                name = (op.stem + "_dekrypteret.pdf"
                        if job.enc_key == pdf_utils.ENC_DECRYPTED else op.name)
                out = _unique_save_path(Path(dest_folder) / name, source_paths)
                if export_formats.ocr_available():
                    if ocr_report:
                        ocr_report()
                    export_formats.add_searchable_ocr_layer(doc)
                doc.save(str(out), garbage=3, deflate=True)
                saved += 1
            else:
                if export_report:
                    export_report(extension)
                out = _unique_save_path(Path(dest_folder) / (op.stem + extension), source_paths)
                res = export_formats.write_document(
                    fmt, doc=doc, out_path=str(out), chapters=chapters, title=op.stem,
                    progress=export_progress, cancel=cancel)
                saved += 1
                missing_pages += res.missing_pages
        except export_formats.ExportCancelled:
            raise
        except export_formats.ExportError as e:
            failed += 1
            logger.info("Kunne ikke eksportere %s: %s", op.name, e)
        except Exception as e:
            failed += 1
            logger.error("Fejl ved gemning af %s: %s", op.name, e)
        finally:
            doc.close()

    return {"saved": saved, "failed": failed, "missing_pages": missing_pages}


def _unique_save_path(out: Path, reserved: set) -> Path:
    """Avoid overwriting a source/input file (in ``reserved``) or an existing file.
    Appends _1, _2, ... to the stem until the path is free."""
    candidate = out
    counter = 1
    while os.path.normcase(os.path.abspath(candidate)) in reserved or candidate.exists():
        candidate = out.with_name(f"{out.stem}_{counter}{out.suffix}")
        counter += 1
    return candidate
