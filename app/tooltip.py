"""Hover-tooltips.

sv-ttk har ingen tooltip, saa den skrives her. Designet er defensivt, fordi
Unite Docs river hele widget-traeet ned og bygger det op igen ved sprogskift:
hvert Tk-kald ligger i ``try/except tk.TclError`` bag et ``winfo_exists()``, og
``<Destroy>`` afmelder den planlagte visning.

i18n: tooltip-tekst er altid ``_("...")``. Genvejshint sendes via den separate
``shortcut=``-parameter og bliver **aldrig** konkateneret ind i en oversaetbar
streng. Dynamisk tekst sendes som en callable, saa ``_()`` genevalueres paa
visningstidspunktet -- ellers ville teksten vaere fastfrosset i opstartssproget.
"""

from __future__ import annotations

import tkinter as tk

from . import theme
from .logging_config import get_logger

logger = get_logger(__name__)


class Tooltip:
    """Én tooltip knyttet til én widget. Kun én er synlig ad gangen."""

    _active: "Tooltip | None" = None
    _instances: "list[Tooltip]" = []

    # ------------------------------------------------------------------ API
    @classmethod
    def attach(cls, widget, text, *, shortcut: str | None = None,
               delay: int | None = None) -> "Tooltip":
        return cls(widget, text, shortcut=shortcut, delay=delay)

    def __init__(self, widget, text, *, shortcut: str | None = None,
                 delay: int | None = None):
        self.widget = widget
        self.text = text                     # str ELLER callable() -> str
        self.shortcut = shortcut
        self.delay = theme.TOOLTIP_DELAY_MS if delay is None else delay
        self._after = None
        self._win = None

        # add="+" saa widgetens egne bindinger overlever.
        try:
            widget.bind("<Enter>", self._on_enter, add="+")
            widget.bind("<Leave>", self._on_leave, add="+")
            widget.bind("<ButtonPress>", self._on_leave, add="+")
            widget.bind("<FocusOut>", self._on_leave, add="+")
            widget.bind("<Destroy>", self._on_destroy, add="+")
        except tk.TclError as e:
            logger.debug("Tooltip-binding fejlede: %s", e)

        Tooltip._instances.append(self)

    def update_text(self, text) -> None:
        """Skift teksten (str eller callable). Virker ogsaa mens den er synlig."""
        self.text = text
        if self._win is not None:
            self._hide()
            self._show()

    def destroy(self) -> None:
        self._cancel()
        self._hide()
        try:
            Tooltip._instances.remove(self)
        except ValueError:
            pass

    @classmethod
    def hide_all(cls) -> None:
        """Afbryd alle planlagte og synlige tooltips.

        Skal kaldes foerst i ``_rebuild_ui_for_language_change`` -- ellers kan en
        ``after``-callback vaekke en destrueret widget.
        """
        for t in list(cls._instances):
            t._cancel()
            t._hide()
        cls._active = None

    # -------------------------------------------------------------- interne
    def _resolve_text(self) -> str:
        try:
            body = self.text() if callable(self.text) else self.text
        except Exception as e:
            logger.debug("Tooltip-tekst fejlede: %s", e)
            return ""
        return "" if body is None else str(body)

    def _on_enter(self, _event=None):
        self._cancel()
        try:
            self._after = self.widget.after(self.delay, self._show)
        except tk.TclError:
            self._after = None

    def _on_leave(self, _event=None):
        self._cancel()
        self._hide()

    def _on_destroy(self, event=None):
        # <Destroy> bobler fra boernewidgets; reagér kun paa vores egen.
        if event is not None and event.widget is not self.widget:
            return
        self.destroy()

    def _cancel(self) -> None:
        if self._after is None:
            return
        try:
            self.widget.after_cancel(self._after)
        except (tk.TclError, ValueError):
            pass
        self._after = None

    def _alive(self) -> bool:
        try:
            return bool(self.widget.winfo_exists())
        except tk.TclError:
            return False

    def _show(self) -> None:
        self._after = None
        if not self._alive():
            return
        body = self._resolve_text()
        if not body:
            return

        if Tooltip._active is not None and Tooltip._active is not self:
            Tooltip._active._hide()

        try:
            win = tk.Toplevel(self.widget)
            win.overrideredirect(True)
            try:
                win.wm_attributes("-topmost", True)
            except tk.TclError:
                pass
            try:
                # Goer vinduet klik-inert paa Windows, saa det aldrig staeler et klik.
                win.wm_attributes("-disabled", True)
            except tk.TclError:
                pass

            # 1 px "kant" = ydre frame i kantfarven med 1 px padding.
            border = tk.Frame(win, background=theme.C["border_strong"], bd=0,
                              highlightthickness=0)
            border.pack(fill="both", expand=True)
            inner = tk.Frame(border, background=theme.C["surface"], bd=0,
                             highlightthickness=0)
            inner.pack(fill="both", expand=True, padx=1, pady=1)

            tk.Label(inner, text=body, background=theme.C["surface"],
                     foreground=theme.C["text"], font=theme.FONTS["base"],
                     justify="left").pack(
                         side="left", pady=4,
                         padx=((8, 0) if self.shortcut else (8, 8)))
            if self.shortcut:
                tk.Label(inner, text=self.shortcut,
                         background=theme.C["surface"],
                         foreground=theme.C["text_muted"],
                         font=theme.FONTS["small"]).pack(side="left",
                                                         padx=(6, 8), pady=4)

            self._win = win
            Tooltip._active = self
            self._place(win)
        except tk.TclError as e:
            logger.debug("Tooltip kunne ikke vises: %s", e)
            self._hide()

    def _place(self, win) -> None:
        """Nede-til-hoejre for widgeten, klemt inden for skaermen."""
        try:
            win.update_idletasks()
            w, h = win.winfo_reqwidth(), win.winfo_reqheight()
            wx, wy = self.widget.winfo_rootx(), self.widget.winfo_rooty()
            wh = self.widget.winfo_height()
            sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()

            x = wx
            y = wy + wh + 6
            if y + h > sh - 4:              # ingen plads under -> vend over
                y = wy - h - 6
            x = max(4, min(x, sw - w - 4))
            y = max(4, y)
            win.wm_geometry("+%d+%d" % (int(x), int(y)))
        except tk.TclError as e:
            logger.debug("Tooltip-placering fejlede: %s", e)

    def _hide(self) -> None:
        win, self._win = self._win, None
        if win is not None:
            try:
                win.destroy()
            except tk.TclError:
                pass
        if Tooltip._active is self:
            Tooltip._active = None
