"""Background page renderer for the page view (Fase 3).

Mirrors the established thumbnail-worker contract: workers NEVER touch Tk — they
produce a PIL image and hand it back to the main thread via ``app._queue`` (the
same 50 ms drain loop the rest of the app uses). Adds three things the page view
needs over the flat thumbnail manager:

* **Priority** — the large preview must render before background tiles, so it is
  requested at priority 0 (a ``PriorityQueue`` orders the rest).
* **Generation drop** — when the page set changes (grid rebuilt) the generation is
  bumped; in-flight requests from an older generation are discarded instead of
  rendering pages that are no longer on screen.
* **LRU image cache** — many pages × many files can be large, so decoded tile
  images are capped and evicted least-recently-used.

Rotation is snapshotted into the request on the main thread (the worker never
reads ``app.rotations`` — that was a latent race in the old path).
"""

from __future__ import annotations

import itertools
import queue
import threading
import time
from collections import OrderedDict

from PIL import Image

from . import pdf_renderer
from . import text_edit
from .logging_config import get_logger

logger = get_logger(__name__)


def cache_key(path, page_index, rotation, box, crop=None, edits=()):
    """Den ENE definition af render-cachens noegleform.

    ``crop`` er med, fordi to PageEdits kan dele kildeside med hver sin
    beskaering -- og fordi sidegitterets "er flisen forældet?"-test sammenligner
    noegler. ``invalidate_path``/``invalidate_page`` roerer kun k[0]/k[1] og er
    derfor upaavirkede af at noeglen bliver laengere.

    ``edits`` er sidens indholdsrettelser (:func:`content_edits_of`); de
    aendrer selve billedet og indgaar med deres uid -- en spec faar ny uid naar
    den rettes, saa uid'en er nok."""
    return (path, page_index, rotation, box, tuple(crop) if crop else None,
            tuple(s.uid for s in edits))


def content_edits_of(page) -> tuple:
    """En ``PageEdit``s indholdsrettelser (Rediger-gruppen) i den raekkefoelge
    de blev lavet -- dem der skal bages ind i billedet."""
    return tuple(a for a in page.annots if a.kind in text_edit.CONTENT_KINDS)


class _LRU:
    """Tiny thread-safe LRU cache of PIL images keyed by (path, index, rotation, box)."""

    def __init__(self, limit: int = 240):
        self._d = OrderedDict()
        self._limit = limit
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self._d:
                self._d.move_to_end(key)
                return self._d[key]
            return None

    def put(self, key, value):
        with self._lock:
            self._d[key] = value
            self._d.move_to_end(key)
            while len(self._d) > self._limit:
                self._d.popitem(last=False)

    def evict_where(self, predicate):
        """Drop entries whose key satisfies predicate(key)."""
        with self._lock:
            for k in [k for k in self._d if predicate(k)]:
                self._d.pop(k, None)

    def clear(self):
        with self._lock:
            self._d.clear()


class PageRenderManager:
    def __init__(self, app, workers: int = 3, cache_limit: int = 450):
        self.app = app
        self._q = queue.PriorityQueue()
        self._stop = threading.Event()
        self._threads = []
        self._workers = workers
        self._seq = itertools.count()          # tiebreaker so payloads never compare
        self._generation = 0
        self._genlock = threading.Lock()
        self.cache = _LRU(cache_limit)
        # Background prefetch (priority >= 2) is paused until this timestamp while
        # the user scrolls, so the CPU-bound workers don't starve the main thread of
        # the GIL (that froze scrolling for up to ~1.6 s).
        self._pause_prefetch_until = 0.0
        # Only ONE prefetch render may run at a time, so the other workers stay free
        # for interactive renders and overall GIL/CPU pressure never freezes the UI.
        self._prefetch_sem = threading.Semaphore(1)

    # --- lifecycle --------------------------------------------------------
    def start(self):
        self._stop.clear()
        self._threads = [t for t in self._threads if t.is_alive()]
        while len(self._threads) < self._workers:
            t = threading.Thread(target=self._loop, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self):
        self._stop.set()
        for _ in range(self._workers):
            try:
                self._q.put_nowait((-1, next(self._seq), None))
            except Exception:
                break

    # --- generation -------------------------------------------------------
    def bump_generation(self) -> int:
        with self._genlock:
            self._generation += 1
            return self._generation

    def _current_generation(self) -> int:
        with self._genlock:
            return self._generation

    # --- activity throttle ------------------------------------------------
    def notify_activity(self, seconds: float = 0.35):
        """Call on scroll: pause background prefetch briefly so the main thread
        keeps the GIL and scrolling stays smooth. High-priority renders (visible
        tiles, preview) are never paused."""
        self._pause_prefetch_until = time.time() + seconds

    # --- requests ---------------------------------------------------------
    def request(self, key, path, passwords, page_index, rotation, box, on_ready,
                *, priority: int = 1, dpi: int = 90, generation: int | None = None,
                crop=None, edits=()):
        """Queue a render. ``on_ready(key, pil_or_none)`` is scheduled on the main
        thread. ``box`` (w, h) downsizes the result (None keeps full size).
        ``priority`` 0 = preview (first), 1 = visible tile, 2 = prefetch.

        ``crop`` indgaar i cache-noeglen paa lige fod med ``rotation``: to sider kan
        dele kildeside men have hver sin beskaering, saa udelades den, faar den ene
        den andens billede."""
        crop = tuple(crop) if crop else None
        edits = tuple(edits)
        cached = self.cache.get(cache_key(path, page_index, rotation, box, crop, edits))
        if cached is not None:
            self.app._queue.put((on_ready, (key, cached)))
            return
        if generation is None:
            generation = self._current_generation()
        self._q.put((priority, next(self._seq),
                     (generation, key, path, tuple(passwords), page_index, rotation,
                      box, dpi, on_ready, crop, edits)))

    def _loop(self):
        while not self._stop.is_set():
            try:
                prio, _seq, payload = self._q.get(timeout=1.0)
            except queue.Empty:
                continue
            if payload is None:
                break
            (generation, key, path, passwords, page_index, rotation, box, dpi,
             on_ready, crop, edits) = payload
            if generation != self._current_generation():
                continue                      # stale — page set changed, drop it

            if prio >= 2:
                # Background prefetch: skip while the user scrolls, and allow only
                # one at a time. If we can't run now, put it back and nap so the main
                # thread (and higher-priority renders) keep the CPU.
                if (time.time() < self._pause_prefetch_until
                        or not self._prefetch_sem.acquire(blocking=False)):
                    self._q.put((prio, next(self._seq), payload))
                    time.sleep(0.03)
                    continue
                try:
                    self._render_one(key, path, passwords, page_index, rotation,
                                     box, dpi, on_ready, crop, edits)
                finally:
                    self._prefetch_sem.release()
                time.sleep(0.005)             # gentle yield so prefetch stays polite
            else:
                self._render_one(key, path, passwords, page_index, rotation, box,
                                 dpi, on_ready, crop, edits)

    def _render_one(self, key, path, passwords, page_index, rotation, box, dpi,
                    on_ready, crop=None, edits=()):
        try:
            # Render at ~target size instead of a fixed dpi (huge speedup for large
            # pages); box is (w, h) of the tile/preview area.
            mpx = max(box) if box else None
            img = pdf_renderer.render_page(path, list(passwords), page_index,
                                           rotation=rotation, dpi=dpi, max_px=mpx,
                                           crop=crop, text_edits=edits)
            if img is not None and box is not None:
                img = img.copy()
                img.thumbnail(box, Image.Resampling.LANCZOS)
            if img is not None:
                self.cache.put(cache_key(path, page_index, rotation, box, crop, edits),
                               img)
            if self._stop.is_set():
                return
            self.app._queue.put((on_ready, (key, img)))
        except Exception as e:
            logger.error("Page render error (%s p%s): %s", path, page_index, e)
            self.app._queue.put((on_ready, (key, None)))

    # --- cache maintenance -------------------------------------------------
    def invalidate_path(self, path):
        self.cache.evict_where(lambda k: k[0] == path)

    def invalidate_page(self, path, page_index):
        self.cache.evict_where(lambda k: k[0] == path and k[1] == page_index)
