import tkinter as tk
from tkinter import ttk
from pathlib import Path
import math
from PIL import Image, ImageTk
from reportlab.lib.pagesizes import A4
from .localization import LocalizationManager

# INTERNATIONALIZATION: Global translation function for preview window
# AI ASSISTANTS: Always wrap user-visible text in _("text") function
_ = LocalizationManager.get_text

# Performance: frozenset gives O(1) membership test vs O(n) tuple scan
_IMAGE_SUFFIXES = frozenset({'.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.tif'})

class PreviewWindow(tk.Toplevel):
    def __init__(self, master, iid: str, size: tuple[int, int]):
        super().__init__(master)
        self.master_app = master
        self.iid = iid
        self.file_path = self.master_app.paths[self.iid]
        self.title(_("Vis fil") + ": " + Path(self.file_path).name)
        self.geometry(f"{size[0]}x{size[1]}")
        self.minsize(400, 300)

        # --- State Variables ---
        self.A4_PORTRAIT_RATIO = math.sqrt(2)
        self.zoom_level = 1.0
        self.original_pil_image = None
        self.tk_photo = None
        self.view_x, self.view_y = 0, 0
        self.last_drag_x, self.last_drag_y = 0, 0
        self.is_cropping = False
        self.crop_start_pos = None
        self.crop_rect_id = None
        self.display_params = {'paste_x': 0, 'paste_y': 0}
        
        # --- Current State ---
        self.current_rotation = self.master_app.rotations.get(self.iid, 0)
        self.current_crop = self.master_app.croppings.get(self.iid)

        # --- Save original states for cancel functionality ---
        self.original_rotation = self.current_rotation
        self.original_crop = self.current_crop

        # Performance: cache the rotated PIL image so repeated zoom/resize/drag calls
        # don't re-rotate the full-resolution image on every display update.
        # Invalidated (set to None) whenever current_rotation changes in _rotate().
        self._rotated_image_cache: Image.Image | None = None
        self._cached_rotation: int = self.current_rotation

        # Performance: initialise to None so hasattr() checks can be replaced
        # with a fast None-equality test throughout the class.
        self.remove_crop_button: tk.Widget | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._cancel_changes)
        self.bind("<Configure>", self._on_resize)
        self.bind("<MouseWheel>", self._on_mouse_wheel)
        
        self.after(50, self._load_and_display_image)

    def _build_ui(self):
        content_frame = ttk.Frame(self)
        content_frame.pack(side="top", fill="both", expand=True)

        toolbar = ttk.Frame(content_frame)
        toolbar.pack(side="top", fill="x", padx=5, pady=(5, 0))

        # Performance: cache icons dict reference locally — avoids repeated attribute lookups
        icons = self.master_app.icons
        ttk.Button(toolbar, text=_("Zoom +"), image=icons.get('zoom_in'), compound="top", command=lambda: self._zoom(1.25)).pack(side="left")
        ttk.Button(toolbar, text=_("Zoom -"), image=icons.get('zoom_out'), compound="top", command=lambda: self._zoom(0.8)).pack(side="left", padx=2)
        ttk.Button(toolbar, text=_("Roter venstre"), image=icons.get('rotate_left'), compound="top", command=lambda: self._rotate(-90)).pack(side="left", padx=(10, 2))
        ttk.Button(toolbar, text=_("Roter højre"), image=icons.get('rotate_right'), compound="top", command=lambda: self._rotate(90)).pack(side="left")

        # Performance: frozenset gives O(1) suffix check (see module-level _IMAGE_SUFFIXES)
        is_image_file = Path(self.file_path).suffix.lower() in _IMAGE_SUFFIXES
        if is_image_file:
            self.crop_button = ttk.Button(toolbar, text=_("Beskær"), image=icons.get('crop'), compound="top", command=self._start_cropping)
            self.crop_button.pack(side="left", padx=(10, 0))
            self.remove_crop_button = ttk.Button(toolbar, text=_("Fjern beskæring"), image=icons.get('remove_crop_preview'), compound="top", command=self._remove_crop, state="disabled")
            self.remove_crop_button.pack(side="left", padx=(2,0))

        ttk.Button(toolbar, text=_("Ok"), image=icons.get('ok'), compound="top", command=self._save_changes).pack(side="left", padx=(10,0))
        ttk.Button(toolbar, text=_("Annuller"), image=icons.get('cancel_preview'), compound="top", command=self._cancel_changes).pack(side="left", padx=(2,0))

        self.image_canvas = tk.Canvas(content_frame, bg="#F0F0F0", highlightthickness=0)
        self.image_canvas.pack(side="top", fill="both", expand=True, padx=5, pady=5)
        self.image_canvas.bind("<ButtonPress-1>", self._on_mouse_press)
        self.image_canvas.bind("<B1-Motion>", self._on_mouse_drag)
        self.image_canvas.bind("<ButtonRelease-1>", self._on_mouse_release)

    def _load_and_display_image(self):
        self.original_pil_image = self.master_app._get_original_pil_image(self.iid)
        if self.original_pil_image:
            self._on_resize()
        else:
            error_msg = _("Filen kunne ikke forhåndsvises") + ":\n" + Path(self.file_path).name + "\n\n(" + _("Måske en krypteret PDF") + "?)"
            self.image_canvas.create_text(self.winfo_width()/2, self.winfo_height()/2, text=error_msg, anchor="center")
    
    def _rotate(self, angle: int):
        if self.is_cropping: return
        self.current_crop = None  # Rotation fjerner beskæring
        self.current_rotation = (self.current_rotation + angle + 360) % 360
        # Performance: invalidate rotation cache so _update_display() re-rotates
        self._rotated_image_cache = None
        self._update_display()
    
    def _zoom(self, factor: float, event=None):
        if self.is_cropping: return
        old_zoom = self.zoom_level
        new_zoom = self.zoom_level * factor
        if self.original_pil_image:
            container_w, container_h = self.image_canvas.winfo_width(), self.image_canvas.winfo_height()
            if container_w > 1 and container_h > 1:
                # Performance: reuse cached rotated image for dimension calculations
                if self._rotated_image_cache is None or self._cached_rotation != self.current_rotation:
                    self._rotated_image_cache = self.original_pil_image.rotate(-self.current_rotation, expand=True)
                    self._cached_rotation = self.current_rotation
                rotated_img = self._rotated_image_cache
                fit_zoom_w = container_w / rotated_img.width
                fit_zoom_h = container_h / rotated_img.height
                min_zoom = min(fit_zoom_w, fit_zoom_h)
                if new_zoom < min_zoom:
                    new_zoom = min_zoom
        self.zoom_level = new_zoom
        if event:
            mouse_x, mouse_y = event.x, event.y
            image_x = self.view_x + mouse_x
            image_y = self.view_y + mouse_y
            self.view_x = image_x * (new_zoom / old_zoom) - mouse_x
            self.view_y = image_y * (new_zoom / old_zoom) - mouse_y
        else:
            center_x = self.image_canvas.winfo_width() / 2
            center_y = self.image_canvas.winfo_height() / 2
            image_x = self.view_x + center_x
            image_y = self.view_y + center_y
            self.view_x = image_x * (new_zoom / old_zoom) - center_x
            self.view_y = image_y * (new_zoom / old_zoom) - center_y
        self._update_display()

    def _on_resize(self, event=None):
        if self.original_pil_image and not self.is_cropping:
            self.zoom_level = 1.0
            container_w, container_h = self.image_canvas.winfo_width(), self.image_canvas.winfo_height()
            if container_w > 1 and container_h > 1:
                # Performance: reuse cached rotated image for dimension calculations
                if self._rotated_image_cache is None or self._cached_rotation != self.current_rotation:
                    self._rotated_image_cache = self.original_pil_image.rotate(-self.current_rotation, expand=True)
                    self._cached_rotation = self.current_rotation
                rotated_img = self._rotated_image_cache
                fit_zoom_w = container_w / rotated_img.width
                fit_zoom_h = container_h / rotated_img.height
                self.zoom_level = min(fit_zoom_w, fit_zoom_h)
        self._update_display()
    
    def _update_display(self):
        if not self.original_pil_image or not self.winfo_exists(): return
        self.image_canvas.delete("all")

        # Performance: reuse cached rotated image when rotation hasn't changed
        if self._rotated_image_cache is None or self._cached_rotation != self.current_rotation:
            self._rotated_image_cache = self.original_pil_image.rotate(-self.current_rotation, expand=True)
            self._cached_rotation = self.current_rotation
        rotated_img = self._rotated_image_cache
        container_w = self.image_canvas.winfo_width()
        container_h = self.image_canvas.winfo_height()
        if container_w < 2 or container_h < 2: return
        
        zoomed_w = int(rotated_img.width * self.zoom_level)
        zoomed_h = int(rotated_img.height * self.zoom_level)
        
        if zoomed_w <= container_w and zoomed_h <= container_h:
            self.view_x, self.view_y = 0, 0
            resized_img = rotated_img.resize((zoomed_w, zoomed_h), Image.Resampling.LANCZOS)
            self.display_params['paste_x'] = (container_w - zoomed_w) // 2
            self.display_params['paste_y'] = (container_h - zoomed_h) // 2
            self.tk_photo = ImageTk.PhotoImage(resized_img)
            self.image_canvas.create_image(self.display_params['paste_x'], self.display_params['paste_y'], anchor='nw', image=self.tk_photo)
        else:
            self.display_params['paste_x'], self.display_params['paste_y'] = 0, 0
            max_view_x = max(0, zoomed_w - container_w)
            max_view_y = max(0, zoomed_h - container_h)
            self.view_x = max(0, min(self.view_x, max_view_x))
            self.view_y = max(0, min(self.view_y, max_view_y))
            zoomed_img = rotated_img.resize((zoomed_w, zoomed_h), Image.Resampling.LANCZOS)
            crop_box = (int(self.view_x), int(self.view_y), int(self.view_x + container_w), int(self.view_y + container_h))
            visible_part = zoomed_img.crop(crop_box)
            self.tk_photo = ImageTk.PhotoImage(visible_part)
            self.image_canvas.create_image(0, 0, anchor='nw', image=self.tk_photo)
        
        self._redisplay_crop_box()

    def _redisplay_crop_box(self):
        if self.crop_rect_id:
            self.image_canvas.delete(self.crop_rect_id)
            self.crop_rect_id = None
        
        if self.current_crop:
            screen_coords = self._transform_original_to_screen_coords(self.current_crop)
            if screen_coords:
                self.crop_rect_id = self.image_canvas.create_rectangle(
                    screen_coords, outline='red', width=2, dash=(4, 4))
            if self.remove_crop_button is not None:
                self.remove_crop_button.config(state="normal")
        else:
            if self.remove_crop_button is not None:
                self.remove_crop_button.config(state="disabled")

    def _on_mouse_press(self, event):
        if self.is_cropping:
            self._on_crop_press(event)
        else:
            self.last_drag_x, self.last_drag_y = event.x, event.y
            self.image_canvas.config(cursor="fleur")

    def _on_mouse_drag(self, event):
        if self.is_cropping:
            self._on_crop_drag(event)
        else:
            dx, dy = event.x - self.last_drag_x, event.y - self.last_drag_y
            self.view_x -= dx
            self.view_y -= dy
            self.last_drag_x, self.last_drag_y = event.x, event.y
            self._update_display()
    
    def _on_mouse_release(self, event):
        if self.is_cropping:
            self._on_crop_release(event)
        else:
            self.image_canvas.config(cursor="")
    
    def _on_mouse_wheel(self, event):
        if self.is_cropping: return
        if event.delta > 0:
            self._zoom(1.25, event)
        else:
            self._zoom(0.8, event)

    def _start_cropping(self):
        self.is_cropping = True
        self.image_canvas.config(cursor="crosshair")

    def _remove_crop(self):
        self.current_crop = None
        if self.crop_rect_id:
            self.image_canvas.delete(self.crop_rect_id)
            self.crop_rect_id = None
        if self.remove_crop_button is not None:
            self.remove_crop_button.config(state="disabled")
        self._update_display()  # Refresh to remove crop box overlay

    def _on_crop_press(self, event):
        self.crop_start_pos = (event.x, event.y)
        if self.crop_rect_id:
            self.image_canvas.delete(self.crop_rect_id)
        self.crop_rect_id = self.image_canvas.create_rectangle(event.x, event.y, event.x, event.y, outline='red', width=2, dash=(4, 4))
    
    def _on_crop_drag(self, event):
        if not self.crop_rect_id: return
        start_x, start_y = self.crop_start_pos
        
        width = abs(event.x - start_x)
        height = width / self.A4_PORTRAIT_RATIO if A4[0] > A4[1] else width * self.A4_PORTRAIT_RATIO
        
        end_x = event.x
        end_y = start_y + height if event.y > start_y else start_y - height

        self.image_canvas.coords(self.crop_rect_id, start_x, start_y, end_x, end_y)
    
    def _on_crop_release(self, event):
        if self.crop_rect_id:
            coords = self.image_canvas.coords(self.crop_rect_id)
            if abs(coords[0] - coords[2]) > 5 and abs(coords[1] - coords[3]) > 5:
                 self.current_crop = self._transform_screen_to_original_coords(coords)
                 if self.remove_crop_button is not None:
                    self.remove_crop_button.config(state="normal")
            else: # Crop too small, discard
                self.image_canvas.delete(self.crop_rect_id)
                self.crop_rect_id = None
                self.current_crop = None

        self.is_cropping = False
        self.image_canvas.config(cursor="")

    def _transform_original_to_screen_coords(self, original_box):
        if not self.original_pil_image: return None
        ox1, oy1, ox2, oy2 = original_box

        # Performance: reuse cached rotated image
        if self._rotated_image_cache is None or self._cached_rotation != self.current_rotation:
            self._rotated_image_cache = self.original_pil_image.rotate(-self.current_rotation, expand=True)
            self._cached_rotation = self.current_rotation
        rotated_img = self._rotated_image_cache
        ow, oh = self.original_pil_image.width, self.original_pil_image.height
        rw, rh = rotated_img.width, rotated_img.height

        coords_rot = []
        for ox, oy in [(ox1, oy1), (ox2, oy2)]:
            if self.current_rotation == 0:
                rx, ry = ox, oy
            elif self.current_rotation == 90:
                rx, ry = oh - oy, ox
            elif self.current_rotation == 180:
                rx, ry = ow - ox, oh - oy
            elif self.current_rotation == 270:
                rx, ry = oy, ow - ox
            coords_rot.append((rx,ry))
        
        rx1, ry1 = coords_rot[0]
        rx2, ry2 = coords_rot[1]

        zx1, zy1 = rx1 * self.zoom_level, ry1 * self.zoom_level
        zx2, zy2 = rx2 * self.zoom_level, ry2 * self.zoom_level
        
        paste_x, paste_y = self.display_params['paste_x'], self.display_params['paste_y']
        sx1 = zx1 - self.view_x + paste_x
        sy1 = zy1 - self.view_y + paste_y
        sx2 = zx2 - self.view_x + paste_x
        sy2 = zy2 - self.view_y + paste_y
        
        return (sx1, sy1, sx2, sy2)

    def _transform_screen_to_original_coords(self, screen_box):
        sx1_raw, sy1_raw, sx2_raw, sy2_raw = screen_box
        sx1, sx2 = min(sx1_raw, sx2_raw), max(sx1_raw, sx2_raw)
        sy1, sy2 = min(sy1_raw, sy2_raw), max(sy1_raw, sy2_raw)
        
        paste_x, paste_y = self.display_params['paste_x'], self.display_params['paste_y']
        
        zx1 = sx1 - paste_x + self.view_x
        zy1 = sy1 - paste_y + self.view_y
        zx2 = sx2 - paste_x + self.view_x
        zy2 = sy2 - paste_y + self.view_y

        rx1 = zx1 / self.zoom_level
        ry1 = zy1 / self.zoom_level
        rx2 = zx2 / self.zoom_level
        ry2 = zy2 / self.zoom_level

        # Performance: reuse cached rotated image
        if self._rotated_image_cache is None or self._cached_rotation != self.current_rotation:
            self._rotated_image_cache = self.original_pil_image.rotate(-self.current_rotation, expand=True)
            self._cached_rotation = self.current_rotation
        rotated_img = self._rotated_image_cache
        ow, oh = self.original_pil_image.width, self.original_pil_image.height
        rw, rh = rotated_img.width, rotated_img.height

        coords = []
        for rx, ry in [(rx1, ry1), (rx2, ry2)]:
            if self.current_rotation == 0:
                ox, oy = rx, ry
            elif self.current_rotation == 90:
                ox, oy = ry, oh - rx
            elif self.current_rotation == 180:
                ox, oy = ow - rx, oh - ry
            elif self.current_rotation == 270:
                ox, oy = rw - ry, rx
            coords.append((ox, oy))
        
        ox1, oy1 = coords[0]
        ox2, oy2 = coords[1]
        final_box = (min(ox1, ox2), min(oy1, oy2), max(ox1, ox2), max(oy1, oy2))
        return tuple(int(c) for c in final_box)

    def _save_changes(self):
        if self.iid not in self.master_app.paths:
            self.destroy()
            return

        self.master_app.rotations[self.iid] = self.current_rotation
        if self.current_crop:
            self.master_app.croppings[self.iid] = self.current_crop
        else:
            self.master_app.croppings.pop(self.iid, None)
        
        # Use thumbnail queue instead of individual threads (Fix for Bug 1)
        self.master_app.thumbnail_manager.queue_request(self.file_path, self.iid)
        self.destroy()

    def _cancel_changes(self):
        if self.iid not in self.master_app.paths:
            self.destroy()
            return

        # Restore original crop and rotation states
        self.master_app.rotations[self.iid] = self.original_rotation
        if self.original_crop:
            self.master_app.croppings[self.iid] = self.original_crop
        else:
            self.master_app.croppings.pop(self.iid, None)
        self.destroy()
