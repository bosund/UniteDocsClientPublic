import tkinter as tk
import threading
import queue
import time
from pathlib import Path
from PIL import Image, ImageTk, ImageFile

from . import pdf_renderer
from . import utils
from .localization import LocalizationManager
from .logging_config import get_logger

logger = get_logger(__name__)

# Ensure PIL handles truncated images
ImageFile.LOAD_TRUNCATED_IMAGES = True

class ThumbnailManager:
    def __init__(self, app_interface):
        self.app = app_interface
        self._thumbnail_threads = set()
        self._thumbnail_thread_lock = threading.Lock()
        self._max_thumbnail_threads = 4
        self._thumbnail_queue = queue.Queue()
        self._thumbnail_worker_thread = None
        self._stop_thumbnail_workers = threading.Event()
        self._encrypted_placeholder_cache = {}

    def start_worker(self):
        """Start (or top up) the pool of thumbnail worker threads."""
        self._stop_thumbnail_workers.clear()
        with self._thumbnail_thread_lock:
            # Drop any dead threads, then top up to the configured pool size.
            self._thumbnail_threads = {t for t in self._thumbnail_threads if t.is_alive()}
            while len(self._thumbnail_threads) < self._max_thumbnail_threads:
                t = threading.Thread(target=self._worker_loop, daemon=True)
                t.start()
                self._thumbnail_threads.add(t)

    def stop_worker(self):
        """Stop and cleanup all worker threads."""
        self._stop_thumbnail_workers.set()
        # Unblock any workers parked on queue.get() so they can exit promptly.
        for _ in range(self._max_thumbnail_threads):
            try:
                self._thumbnail_queue.put_nowait(None)
            except Exception:
                break

        with self._thumbnail_thread_lock:
            threads = list(self._thumbnail_threads)
        for t in threads:
            if t.is_alive():
                t.join(timeout=2.0)
        self._cleanup_threads()

        # Clear queue
        while not self._thumbnail_queue.empty():
            try:
                self._thumbnail_queue.get_nowait()
            except queue.Empty:
                break

    def queue_request(self, file_path, iid):
        """Add a thumbnail request to the queue.

        Called from the main thread. The password list is snapshotted HERE so
        worker threads never call app._get_all_passwords(): that method can
        fall through to reading the Tk password widget (_pwlist), and Tk must
        never be touched from a worker thread (risk of native crash).
        """
        passwords = self.app._get_all_passwords()
        self._thumbnail_queue.put((file_path, iid, passwords))

    def _worker_loop(self):
        """Worker loop: render thumbnails off the main thread.

        Workers must NOT touch Tk (it is not thread-safe). They produce a PIL
        image (or None) and hand it to the main thread, which builds the
        PhotoImage and decides whether to show the encrypted placeholder. This
        is what makes running several workers in parallel safe.
        """
        while not self._stop_thumbnail_workers.is_set():
            try:
                request = self._thumbnail_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if request is None:
                break
            file_path, iid, passwords = request
            try:
                # Cheap, thread-safe guard (plain dict) to skip items already
                # removed. The main thread re-checks before displaying.
                if iid not in self.app.paths:
                    continue
                pil_image = self._render_thumbnail_image(file_path, iid, passwords)
                if self._stop_thumbnail_workers.is_set():
                    continue
                if hasattr(self.app, '_queue'):
                    # Always hand back (even None): the main thread substitutes
                    # the encrypted placeholder when no render was produced.
                    self.app._queue.put((self.app._update_tree_item_thumbnail, (iid, pil_image)))
            except Exception as e:
                logger.error("Thumbnail worker error: %s", e)

    def _cleanup_threads(self):
        """Drop finished worker threads from the tracking set."""
        with self._thumbnail_thread_lock:
            self._thumbnail_threads = {t for t in self._thumbnail_threads if t.is_alive()}

    def periodic_cleanup(self):
        """Run periodic cleanup tasks."""
        try:
            self._cleanup_threads()
            
            # Cleanup cache for deleted items
            # Performance: list comprehension instead of manual append loop
            items_to_remove = [
                iid for iid in self.app.thumbnail_cache
                if not self.app.tree.exists(iid) or iid not in self.app.paths
            ]
            for iid in items_to_remove:
                self.app.thumbnail_cache.pop(iid, None)
            
            self._cleanup_old_temp_files()
        except Exception as e:
            logger.error("Periodic cleanup error: %s", e)
        finally:
            if not self._stop_thumbnail_workers.is_set():
                self.app.after(30000, self.periodic_cleanup)

    def _render_thumbnail_image(self, file_path: str, iid: str, passwords: list[str], size=(50, 50)) -> Image.Image | None:
        """Render and post-process a thumbnail as a PIL image (no Tk access).

        'passwords' is snapshotted on the main thread in queue_request() —
        never call app._get_all_passwords() from here (it may read a Tk widget).

        Returns a PIL.Image on success, or None if the file could not be
        rendered (e.g. still-encrypted). The main thread then substitutes the
        encrypted placeholder where appropriate.
        """
        try:
            pil_image = pdf_renderer.render_first_page(file_path, passwords, dpi=150)
            if pil_image is None:
                return None

            crop_box = self.app.croppings.get(iid)
            rotation = self.app.rotations.get(iid, 0)

            if crop_box:
                pil_image = pil_image.crop(crop_box)
            if rotation != 0:
                pil_image = pil_image.rotate(-rotation, expand=True)

            pil_image = pil_image.copy()
            pil_image.thumbnail(size, Image.Resampling.LANCZOS)
            return pil_image
        except Exception as e:
            logger.debug("Kunne ikke lave thumbnail for %s: %s", Path(file_path).name, e)
            return None

    def _get_encrypted_placeholder(self, size: tuple[int,int]) -> ImageTk.PhotoImage:
        if size in self._encrypted_placeholder_cache:
            return self._encrypted_placeholder_cache[size]
        try:
            bg = Image.new('RGBA', size, (240,240,240,255))
            lock_icon_path = utils.resource_path('icons/lock.png')
            lock_img = Image.open(lock_icon_path).convert('RGBA')
            target_side = int(min(size) * 0.6)
            lock_img = lock_img.resize((target_side, target_side), Image.Resampling.LANCZOS)
            lx = (size[0] - target_side)//2
            ly = (size[1] - target_side)//2
            bg.alpha_composite(lock_img, dest=(lx, ly))
            photo = ImageTk.PhotoImage(bg)
            self._encrypted_placeholder_cache[size] = photo
            return photo
        except Exception:
            img = Image.new('RGB', size, (200,200,200))
            photo = ImageTk.PhotoImage(img)
            self._encrypted_placeholder_cache[size] = photo
            return photo

    def cleanup_temp_files(self):
        """Cleanup all temp files (shutdown)."""
        self._cleanup_files_logic(force_all=True)

    def _cleanup_old_temp_files(self):
        """Cleanup only old temp files."""
        self._cleanup_files_logic(force_all=False)

    def _cleanup_files_logic(self, force_all=False):
        try:
            base_data_dir = utils.get_app_data_path()
            if not base_data_dir.exists():
                return
                
            current_time = time.time()
            max_age = 3600 # 1 hour
            
            for item in base_data_dir.iterdir():
                try:
                    should_delete = force_all
                    if not should_delete:
                        # Check age
                        if item.is_dir() and item.name.startswith('split_cache_'):
                             should_delete = (current_time - item.stat().st_mtime > max_age)
                        elif item.is_file() and item.name.endswith('.tmp'):
                             should_delete = (current_time - item.stat().st_mtime > max_age)
                    
                    if should_delete:
                        if item.is_dir() and item.name.startswith('split_cache_'):
                            utils._force_rmtree(item)
                        elif item.is_file() and item.name.endswith('.tmp'):
                            item.unlink()
                except (PermissionError, OSError) as e:
                    # Often fails if file is in use, ignore
                    pass
        except Exception as e:
            logger.error("Error in temp file cleanup: %s", e)
