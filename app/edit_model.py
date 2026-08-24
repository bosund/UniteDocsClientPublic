"""In-memory edit model for Unite Docs (Fase 1).

This module is the *single source of truth* for what the user is composing. It is
deliberately **Tk-free** (pure Python) so it can be unit-tested headless from
``tests/test_suite.py``.

Design in one sentence: a document is a list of :class:`FileEntry`, each owning a
list of :class:`PageEdit`; the Treeview in ``main_app`` becomes a *rendered view*
of ``EditModel.files`` rather than the state itself.

Key invariants (see docs/project_context.md and the plan):

* All annotation geometry is stored in **unrotated source-page points**, so
  rotation is purely a display/`/Rotate` concern and annotations survive rotation,
  insertion into the merged document and view switches without any transform at
  save time.
* Page deletion is a *hard removal* from ``entry.pages`` (not a tombstone) so
  ``len(entry.pages)`` is exactly the "Sider" value with no filtering anywhere.
* ``AnnotationSpec`` is frozen and ``PageEdit`` is mutated only by whole-field
  assignment, which keeps undo records small and aliasing-safe.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# Source-file kinds. Images become exactly one page via _image_to_pdf_a4 at save.
KIND_PDF = "pdf"
KIND_IMAGE = "image"


@dataclass(frozen=True)
class AnnotationSpec:
    """A serialisable annotation.

    NEVER a live ``pymupdf.Annot`` — those die with their document. Geometry is in
    **unrotated source-page points** (see module docstring / plan §1.3).
    """

    kind: str                                   # annotations.ANNOT_* constant
    rects: tuple = ()                           # ((x0, y0, x1, y1), ...)
    strokes: tuple = ()                         # ink: tuple of polylines
    text: str = ""
    color: tuple = (1.0, 1.0, 0.0)
    fill: tuple | None = None
    width: float = 1.0
    opacity: float = 1.0
    fontsize: float = 11.0
    # Redaction reason / entity type ("CPR", "NAVN", ...). Empty for manual annots;
    # set by search / select-all / Presidio so a review UI can group by reason.
    label: str = ""
    # "manual" | "search" | "select_all" | "presidio" — provenance for review.
    source: str = "manual"
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class PageEdit:
    """One page in the composition, pointing back at its source file/page."""

    src_path: str
    src_index: int                    # 0-based page in the SOURCE file, never renumbered
    rotation: int = 0                 # user DELTA on top of the page's own /Rotate
    # Beskaering: (x0, y0, x1, y1) i UROTEREDE kildeenheder med y NEDAD -- samme rum
    # som page_geometry/ViewTransform/AnnotationSpec.rects. For PDF er enheden
    # punkter, for billeder PIL-pixels. Rotation er stadig rent en visningssag, saa
    # en crop bliver haengende paa samme papiromraade naar siden roteres.
    # Konverteres foerst hos de to forbrugere: get_pixmap(clip=) ved render og
    # page.set_cropbox() ved gem (se save_pipeline._cropbox_from_view_rect).
    crop: tuple | None = None
    annots: tuple = ()                # tuple[AnnotationSpec, ...]
    uid: str = field(default_factory=lambda: uuid.uuid4().hex)


@dataclass
class FileEntry:
    """A source file: a first-class level above pages (grouping, per-file
    encryption status, images that have no real pages until save)."""

    iid: str                          # SAME string as the Treeview iid
    path: str
    kind: str                         # KIND_PDF | KIND_IMAGE
    enc_key: str                      # pdf_utils.ENC_* — the stable key, not a label
    creation_date: str = ""
    size_bytes: int = 0               # os.path.getsize; 0 = ukendt. Kun til sortering.
    # iid paa ROD-moderfilen hvis denne entry er en side der er trukket ud som sin
    # egen fil. "" = en rigtig fil. Peger ALDRIG paa et andet barn (se
    # extract_pages), saa grupperingen i sort_order er praecis eet niveau dyb.
    origin_iid: str = ""
    source_page_count: int = 0
    pages: list = field(default_factory=list)   # list[PageEdit]
    # False for still-encrypted / not-yet-enumerated files (one locked placeholder).
    pages_loaded: bool = False

    @property
    def page_count(self) -> str:
        """The value the "Sider" column shows.

        Empty for images. Once pages are populated it reflects ``len(pages)`` (so a
        corrected count after deletions shows with no filtering). Before population
        (file view only, still-encrypted, etc.) it falls back to the disk page
        count read at add time, so the file view is unchanged from today."""
        if self.kind == KIND_IMAGE:
            return ""
        if self.pages_loaded:
            return str(len(self.pages))
        return str(self.source_page_count) if self.source_page_count else ""


@dataclass
class OutputPage:
    """One flattened, ordered page for the save pipeline (Fase 2)."""

    src_path: str
    src_index: int
    kind: str
    rotation: int
    crop: tuple | None
    annots: tuple
    file_title: str                   # source file stem, used for chapter titles


class EditModel:
    """Ordered collection of :class:`FileEntry`; the app's single source of truth."""

    def __init__(self):
        self.files: list = []          # list[FileEntry]

    # --- lookup -----------------------------------------------------------
    def entry_by_iid(self, iid: str) -> "FileEntry | None":
        for f in self.files:
            if f.iid == iid:
                return f
        return None

    def index_of_iid(self, iid: str) -> int:
        for i, f in enumerate(self.files):
            if f.iid == iid:
                return i
        return -1

    def page_by_uid(self, uid: str) -> "tuple[FileEntry, PageEdit] | None":
        for f in self.files:
            for p in f.pages:
                if p.uid == uid:
                    return f, p
        return None

    # --- file mutation ----------------------------------------------------
    def add_file(self, entry: FileEntry, index: int | None = None) -> None:
        if index is None:
            self.files.append(entry)
        else:
            self.files.insert(index, entry)

    def remove_file(self, iid: str) -> "tuple[int, FileEntry] | None":
        """Remove and return ``(index, entry)`` so undo can reinsert it exactly."""
        idx = self.index_of_iid(iid)
        if idx < 0:
            return None
        entry = self.files.pop(idx)
        return idx, entry

    def move_file(self, iid: str, new_index: int) -> None:
        idx = self.index_of_iid(iid)
        if idx < 0:
            return
        entry = self.files.pop(idx)
        self.files.insert(max(0, min(new_index, len(self.files))), entry)

    def reorder_files(self, iids: list[str]) -> None:
        """Reorder ``self.files`` to match the given iid order (drag/sort sync)."""
        by_iid = {f.iid: f for f in self.files}
        new_order = [by_iid[i] for i in iids if i in by_iid]
        # Keep any files not named in `iids` (defensive) appended in old order.
        for f in self.files:
            if f not in new_order:
                new_order.append(f)
        self.files = new_order

    # --- page mutation ----------------------------------------------------
    def populate_pages(self, entry: FileEntry, page_count: int) -> None:
        """Build one PageEdit per source page. Call from a worker under PDF_LOCK
        (this method itself does no I/O — pass a page_count read there)."""
        entry.source_page_count = page_count
        entry.pages = [PageEdit(src_path=entry.path, src_index=i) for i in range(page_count)]
        entry.pages_loaded = True

    def populate_image(self, entry: FileEntry) -> None:
        """Images are exactly one page (src_index=0); rotation/crop apply at save."""
        entry.source_page_count = 1
        entry.pages = [PageEdit(src_path=entry.path, src_index=0)]
        entry.pages_loaded = True

    def remove_page(self, uid: str) -> "tuple[int, int, PageEdit] | None":
        """Hard-remove a page. Returns ``(file_index, position, page)`` so undo can
        reinsert the exact object at its old spot."""
        for fi, f in enumerate(self.files):
            for pi, p in enumerate(f.pages):
                if p.uid == uid:
                    del f.pages[pi]
                    return fi, pi, p
        return None

    def insert_page(self, file_index: int, position: int, page: PageEdit) -> None:
        f = self.files[file_index]
        f.pages.insert(max(0, min(position, len(f.pages))), page)

    def rotate_page(self, uid: str, delta: int) -> None:
        found = self.page_by_uid(uid)
        if found:
            _, p = found
            p.rotation = (p.rotation + delta) % 360

    def set_page_rotation(self, uid: str, rotation: int) -> None:
        found = self.page_by_uid(uid)
        if found:
            _, p = found
            p.rotation = rotation % 360

    def set_page_annots(self, uid: str, annots: tuple) -> None:
        """Whole-field replacement (aliasing-safe; keeps undo records minimal)."""
        found = self.page_by_uid(uid)
        if found:
            _, p = found
            p.annots = tuple(annots)

    def add_annotation(self, uid: str, spec: AnnotationSpec) -> None:
        found = self.page_by_uid(uid)
        if found:
            _, p = found
            p.annots = p.annots + (spec,)

    def remove_annotation(self, uid: str, annot_uid: str) -> None:
        found = self.page_by_uid(uid)
        if found:
            _, p = found
            p.annots = tuple(a for a in p.annots if a.uid != annot_uid)

    def set_page_crop(self, uid: str, crop: "tuple | None") -> None:
        """Whole-field replacement, like set_page_annots (aliasing-safe)."""
        found = self.page_by_uid(uid)
        if found:
            _, p = found
            p.crop = tuple(crop) if crop else None

    # --- page moves / extraction -----------------------------------------
    def pages_index(self) -> dict:
        """``uid -> (FileEntry, PageEdit)`` for hele modellen.

        ``page_by_uid`` er O(filer x sider); traek-og-slip's hit-test kalder den
        ved hver musebevaegelse. Byg denne EEN gang pr. gestus i stedet."""
        out = {}
        for f in self.files:
            for p in f.pages:
                out[p.uid] = (f, p)
        return out

    def ordered_pages(self, uids) -> list:
        """De naevnte uids som PageEdit-objekter, i nuvaerende modelraekkefoelge."""
        want = set(uids)
        return [p for f in self.files for p in f.pages if p.uid in want]

    def _detach(self, pages: list, *, keep_iid: str | None = None) -> None:
        """Fjern ``pages`` fra deres ejerfiler og drop filer der bliver tomme.

        ``keep_iid`` droppes aldrig selv om den toemmes -- den er destinationen
        for en flytning og faar siderne tilbage om et oejeblik. En fil med
        ``pages_loaded=False`` har lovligt tom sideliste (krypteret/ikke laest
        endnu) og roeres derfor ikke."""
        drop = {id(p) for p in pages}
        for f in self.files:
            if any(id(p) in drop for p in f.pages):
                f.pages = [p for p in f.pages if id(p) not in drop]
        self.files = [f for f in self.files
                      if f.pages or not f.pages_loaded or f.iid == keep_iid]

    def _after_child_block(self, root_iid: str, fallback: int) -> int:
        """Indekset lige EFTER ``root_iid`` og dens klaebende boern."""
        i = self.index_of_iid(root_iid)
        if i < 0:
            return max(0, min(fallback, len(self.files)))
        j = i + 1
        while j < len(self.files) and self.files[j].origin_iid == root_iid:
            j += 1
        return j

    def move_pages(self, uids, dst_iid: str, position: int) -> bool:
        """Flyt sider til ``dst_iid`` paa ``position``.

        ``position`` er indeks i destinationens sideliste **efter** at de flyttede
        sider er taget ud -- det fjerner den klassiske off-by-one naar man flytter
        nedad inden i samme fil."""
        if self.entry_by_iid(dst_iid) is None:
            return False
        pages = self.ordered_pages(uids)
        if not pages:
            return False
        self._detach(pages, keep_iid=dst_iid)
        dst = self.entry_by_iid(dst_iid)
        if dst is None:
            return False
        pos = max(0, min(int(position), len(dst.pages)))
        dst.pages[pos:pos] = pages
        dst.pages_loaded = True
        return True

    def extract_pages(self, uids, *, at_index: int | None = None,
                      new_iid: str | None = None) -> "FileEntry | None":
        """Riv sider ud som deres egen FileEntry.

        Den nye entry genbruger moderens ``path``: ``save_pipeline.build_jobs``
        laeser ``p.src_path`` pr. side og bruger kun ``f.path`` til own-testen og
        kapiteltitlen, saa en syntetisk sti ville knaekke ``kind_for_path`` og
        ``_src_runs``. ``origin_iid`` peger altid paa ROD-moderen, saa
        grupperingen i :func:`sort_order` er praecis eet niveau dyb."""
        pages = self.ordered_pages(uids)
        if not pages:
            return None
        found = self.page_by_uid(pages[0].uid)
        if found is None:
            return None
        mother, _p = found
        root_iid = mother.origin_iid or mother.iid
        fallback = self.index_of_iid(mother.iid)
        entry = FileEntry(
            iid=new_iid or uuid.uuid4().hex,
            path=mother.path,
            kind=mother.kind,
            enc_key=mother.enc_key,
            creation_date=mother.creation_date,
            size_bytes=mother.size_bytes,
            origin_iid=root_iid,
            source_page_count=mother.source_page_count,
            pages=pages,
            pages_loaded=True,
        )
        self._detach(pages)
        if at_index is None:
            at_index = self._after_child_block(root_iid, fallback)
        self.files.insert(max(0, min(int(at_index), len(self.files))), entry)
        return entry

    # --- derived views ----------------------------------------------------
    def flatten(self) -> list:
        """Flat ordered ``[(FileEntry, PageEdit)]`` across all loaded files."""
        out = []
        for f in self.files:
            for p in f.pages:
                out.append((f, p))
        return out

    def build_output_plan(self) -> list:
        """Ordered ``[OutputPage]`` for the save pipeline. Only emits files whose
        pages are loaded; encrypted/unreadable files are handled by the pipeline
        caller (it still emits an error page for them)."""
        from pathlib import Path

        plan = []
        for f in self.files:
            if not f.pages_loaded:
                continue
            title = Path(f.path).stem
            for p in f.pages:
                plan.append(OutputPage(
                    src_path=p.src_path,
                    src_index=p.src_index,
                    kind=f.kind,
                    rotation=p.rotation,
                    crop=p.crop,
                    annots=p.annots,
                    file_title=title,
                ))
        return plan


def kind_for_path(path: str) -> str:
    """Classify a path as KIND_PDF or KIND_IMAGE by suffix."""
    from pathlib import Path
    return KIND_PDF if Path(path).suffix.lower() == ".pdf" else KIND_IMAGE


# --- Undo/redo command factories (Fase 5) ---------------------------------
# These build undo_stack.Command objects that mutate ONLY the model. UI refresh
# is driven by the caller (the app resyncs its views after do/undo/redo). Kept
# here (next to the mutators) per the plan; importing Command from undo_stack is
# safe because undo_stack imports nothing from this module.
from .undo_stack import Command


def rotate_page_cmd(model: "EditModel", uid: str, delta: int) -> Command:
    """Rotate one page by +/-delta. Coalesces so a quick multi-step spin on the
    same page is a single undo."""
    def do():
        model.rotate_page(uid, delta)

    def undo():
        model.rotate_page(uid, -delta)

    return Command("Roter side", do, undo, coalesce_key="rotate:%s" % uid)


def delete_page_cmd(model: "EditModel", uid: str) -> Command:
    """Hard-delete a page. If it was the file's last page, the whole FileEntry is
    removed too; undo restores the exact objects at their original positions."""
    st: dict = {}

    def do():
        st.clear()
        removed = model.remove_page(uid)
        if not removed:
            return
        fi, pos, page = removed
        st.update(fi=fi, pos=pos, page=page, file=None, file_index=None)
        entry = model.files[fi] if fi < len(model.files) else None
        if entry is not None and not entry.pages:
            st["file_index"] = model.index_of_iid(entry.iid)
            model.remove_file(entry.iid)
            st["file"] = entry

    def undo():
        if not st:
            return
        if st.get("file") is not None:
            model.add_file(st["file"], st["file_index"])
            fi = model.index_of_iid(st["file"].iid)
            model.insert_page(fi, st["pos"], st["page"])
        else:
            model.insert_page(st["fi"], st["pos"], st["page"])

    return Command("Slet side", do, undo)


def add_annotation_cmd(model: "EditModel", uid: str, spec: "AnnotationSpec") -> Command:
    def do():
        model.add_annotation(uid, spec)

    def undo():
        model.remove_annotation(uid, spec.uid)

    return Command("Tilføj annotation", do, undo)


def remove_annotation_cmd(model: "EditModel", uid: str, spec: "AnnotationSpec") -> Command:
    def do():
        model.remove_annotation(uid, spec.uid)

    def undo():
        model.add_annotation(uid, spec)

    return Command("Fjern annotation", do, undo)


def add_annotations_batch_cmd(model: "EditModel", items) -> Command:
    """One undo step for a whole batch of annotations (e.g. redact-all-matches).
    ``items`` is an iterable of ``(page_uid, AnnotationSpec)``."""
    items = list(items)

    def do():
        for uid, spec in items:
            model.add_annotation(uid, spec)

    def undo():
        for uid, spec in items:
            model.remove_annotation(uid, spec.uid)

    return Command("Tilføj annotationer", do, undo)


def reorder_files_cmd(model: "EditModel", new_order_iids: list) -> Command:
    old_order = [f.iid for f in model.files]

    def do():
        model.reorder_files(list(new_order_iids))

    def undo():
        model.reorder_files(old_order)

    return Command("Omordn filer", do, undo)


def insert_pages_cmd(model: "EditModel", file_index: int, position: int, pages: list) -> Command:
    """Indsæt genererede sider midt i en fils sideliste ("Indsæt side").

    Siderne peger på en ANDEN ``src_path`` end den fil de ligger i — det er
    tilladt og er grunden til at ``PageEdit`` har sin egen ``src_path``.
    Render- og gem-stien læser altid ``page.src_path``, aldrig ``entry.path``.
    """
    uids = [p.uid for p in pages]

    def do():
        for offset, page in enumerate(pages):
            model.insert_page(file_index, position + offset, page)

    def undo():
        for uid in uids:
            model.remove_page(uid)

    return Command("Indsæt side", do, undo)


# --- Sortering (ren, Tk-fri, headless-testbar) -----------------------------
SORT_REVERSE = "reverse"
SORT_DATE = "date"
SORT_NAME = "name"
SORT_SIZE = "size"


def _sort_keyfn(key: str):
    from pathlib import Path
    if key == SORT_NAME:
        return lambda f: (Path(f.path).name.casefold(),)
    if key == SORT_SIZE:
        return lambda f: (f.size_bytes,)
    # Dato: TOMME datoer sorteres SIDST stigende, saa en ENC_ERROR-fil uden dato
    # ikke laegger sig oeverst i listen.
    return lambda f: (f.creation_date == "", f.creation_date)


def sort_order(files: list, key: str, reverse: bool = False) -> list:
    """Ny iid-raekkefoelge for ``files``, klar til :func:`reorder_files_cmd`.

    En udtrukket side-fil (``origin_iid``) klaeber altid umiddelbart EFTER sin
    moder, uanset noegle og retning; boernenes indbyrdes raekkefoelge roeres ikke.
    En dinglende ``origin_iid`` (moderen er slettet) behandles som en rod.
    """
    live = {f.iid for f in files}
    roots, kids = [], {}
    for f in files:
        if f.origin_iid and f.origin_iid in live:
            kids.setdefault(f.origin_iid, []).append(f)
        else:
            roots.append(f)
    if key == SORT_REVERSE:
        roots = list(reversed(roots))
    else:
        roots = sorted(roots, key=_sort_keyfn(key))
        if reverse:
            roots.reverse()
    out = []
    for r in roots:
        out.append(r.iid)
        out.extend(c.iid for c in kids.get(r.iid, []))
    if len(out) != len(files):
        # Defensivt: et barn hvis origin peger paa et andet barn ville ellers
        # falde ud af listen. Behold det i gammel raekkefoelge til sidst.
        seen = set(out)
        out.extend(f.iid for f in files if f.iid not in seen)
    return out


def file_blocks(model: "EditModel") -> list:
    """``model.files`` grupperet som ``[[rod, barn, barn], [rod], ...]``."""
    live = {f.iid for f in model.files}
    blocks, index = [], {}
    for f in model.files:
        parent = f.origin_iid if (f.origin_iid and f.origin_iid in live) else ""
        if parent and parent in index:
            blocks[index[parent]].append(f)
        else:
            index[f.iid] = len(blocks)
            blocks.append([f])
    return blocks


# --- Pil-planlaegger -------------------------------------------------------
def plan_nudge(model: "EditModel", uids: list, direction: int):
    """Hvor skal et pile-tryk flytte markeringen hen?

    ``direction`` +1 = ned/frem, -1 = op/tilbage. Returnerer
    ``("move", dst_iid, position)`` | ``("extract", at_index)`` | ``None``.

    Markeringen behandles som EEN blok: den flyttes samlet og bliver dermed
    sammenhaengende efter foerste tryk. Naar blokken staar ved filens kant,
    flytter naeste tryk den over i nabofilen -- og findes der ingen nabofil i den
    retning, rives den ud som sin egen fil.
    """
    pages = model.ordered_pages(uids)
    if not pages or direction == 0:
        return None
    first = model.page_by_uid(pages[0].uid)
    last = model.page_by_uid(pages[-1].uid)
    if first is None or last is None:
        return None
    f_first, p_first = first
    f_last, p_last = last
    if f_first is not f_last:
        # Markeringen spaender over flere filer: saml den foerst dér hvor blokken
        # begynder, frem for at gaette en retning.
        return ("move", f_first.iid, f_first.pages.index(p_first))

    entry = f_first
    a = entry.pages.index(p_first)
    b = entry.pages.index(p_last)
    j = model.index_of_iid(entry.iid)
    whole_file = len(pages) == len(entry.pages)

    if direction > 0:
        if b < len(entry.pages) - 1:
            return ("move", entry.iid, a + 1)
        nxt = model.files[j + 1] if j + 1 < len(model.files) else None
        if nxt is not None and nxt.pages_loaded:
            return ("move", nxt.iid, 0)
        # Ingen nabofil: bliv din egen fil. Er HELE filen markeret, ville det
        # slette F og lave en identisk fil med nyt iid -- en no-op der stadig
        # braender et undo-trin og oedelaegger markeringen.
        return None if whole_file else ("extract", j + 1)

    if a > 0:
        return ("move", entry.iid, a - 1)
    prv = model.files[j - 1] if j - 1 >= 0 else None
    if prv is not None and prv.pages_loaded:
        return ("move", prv.iid, len(prv.pages))
    return None if whole_file else ("extract", j)


# --- Struktur-kommandoer ---------------------------------------------------
def _snapshot(model: "EditModel") -> list:
    return [(f, list(f.pages)) for f in model.files]


def _restore(model: "EditModel", snap: list) -> None:
    model.files = [f for f, _ in snap]
    for f, pages in snap:
        f.pages = list(pages)


def structural_cmd(model: "EditModel", label_key: str, mutate) -> Command:
    """Undo/redo for enhver aendring der roerer STRUKTUREN (hvilke filer der
    findes, og hvilke sider de ejer).

    Flyt/udtraek/sortering kan baade skabe og fjerne ``FileEntry``-objekter og
    omordne to niveauer paa een gang; haandskrevne inverse lukninger pr.
    operation overlever ikke kombinationerne. Denne kommando gemmer i stedet
    listestrukturen foer og efter -- kun REFERENCER, saa ``FileEntry.iid`` og
    ``PageEdit.uid`` beholder deres objektidentitet (som ``page_view.active`` og
    ``page_canvas.by_uid`` noegler paa), og ingen annotations-tupler kopieres.

    Redo genafspiller ``after``-snapshottet i stedet for at koere ``mutate()``
    igen: en gen-koert udtraekning ville lave et NYT ``uuid4()``-iid og dermed
    goere ethvert barn der peger paa det foerste foraeldreloest.
    """
    st: dict = {}

    def do():
        if "after" in st:
            _restore(model, st["after"])
            return
        st["before"] = _snapshot(model)
        mutate()
        st["after"] = _snapshot(model)

    def undo():
        if "before" in st:
            _restore(model, st["before"])

    return Command(label_key, do, undo)


def move_pages_cmd(model: "EditModel", uids: list, dst_iid: str, position: int) -> Command:
    uids = list(uids)
    return structural_cmd(model, "Flyt side",
                          lambda: model.move_pages(uids, dst_iid, position))


def extract_pages_cmd(model: "EditModel", uids: list, at_index=None) -> Command:
    uids = list(uids)
    # iid'et laases HER, saa redo genbruger det samme. Ellers ville boern der
    # peger paa det blive foraeldreloese efter en undo/redo-runde.
    new_iid = uuid.uuid4().hex
    return structural_cmd(
        model, "Traek side ud",
        lambda: model.extract_pages(uids, at_index=at_index, new_iid=new_iid))


def nudge_pages_cmd(model: "EditModel", uids: list, direction: int):
    """Kommandoen for et pile-tryk, eller None hvis trykket er en no-op."""
    plan = plan_nudge(model, uids, direction)
    if plan is None:
        return None
    if plan[0] == "move":
        return move_pages_cmd(model, uids, plan[1], plan[2])
    return extract_pages_cmd(model, uids, at_index=plan[1])


def delete_pages_cmd(model: "EditModel", uids: list) -> Command:
    """EET undo-trin for en hel multi-markering (modsat delete_page_cmd pr. side)."""
    uids = list(uids)

    def mutate():
        for uid in uids:
            model.remove_page(uid)
        model.files = [f for f in model.files if f.pages or not f.pages_loaded]

    return structural_cmd(model, "Slet sider", mutate)


def rotate_pages_cmd(model: "EditModel", uids: list, delta: int) -> Command:
    """Roter en hel markering som EEN undo-handling."""
    uids = list(uids)

    def do():
        for uid in uids:
            model.rotate_page(uid, delta)

    def undo():
        for uid in uids:
            model.rotate_page(uid, -delta)

    return Command("Roter side", do, undo,
                   coalesce_key="rotate:" + "|".join(sorted(uids)))


def set_pages_crop_cmd(model: "EditModel", items) -> Command:
    """``items`` er ``[(page_uid, crop_or_None)]`` i uroterede kildeenheder."""
    items = [(u, (tuple(c) if c else None)) for u, c in items]
    old: dict = {}

    def do():
        if not old:
            for uid, _c in items:
                found = model.page_by_uid(uid)
                old[uid] = found[1].crop if found else None
        for uid, crop in items:
            model.set_page_crop(uid, crop)

    def undo():
        for uid, _c in items:
            model.set_page_crop(uid, old.get(uid))

    label = "Beskaer side" if any(c for _u, c in items) else "Fjern beskaering"
    return Command(label, do, undo)


def sort_files_cmd(model: "EditModel", key: str, reverse: bool = False) -> Command:
    return reorder_files_cmd(model, sort_order(model.files, key, reverse))


def move_files_cmd(model: "EditModel", iids: list, direction: int) -> Command:
    """Flyt hele filer een plads. Klaebende boern foelger altid deres moder."""
    want = set(iids)

    def mutate():
        blocks = file_blocks(model)
        hits = [i for i, b in enumerate(blocks) if b[0].iid in want]
        if not hits:
            return
        for i in sorted(hits, reverse=direction > 0):
            j = i + (1 if direction > 0 else -1)
            if 0 <= j < len(blocks):
                blocks[i], blocks[j] = blocks[j], blocks[i]
        model.files = [f for b in blocks for f in b]

    return structural_cmd(model, "Flyt fil", mutate)
