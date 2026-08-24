"""Undo/redo for Unite Docs (Fase 5).

Command pattern with **inverse closures**, not snapshots (see plan §3 for the two
concrete reasons): object identity matters — the page grid keys its tiles and
selection on ``PageEdit.uid`` — and a full snapshot would copy every page's
annotation tuples on every edit, which is exactly when the model is large.
Command records hold only the minimal before/after data, which is also what makes
coalescing (e.g. a 360° rotation spin = one undo) possible.

This module is deliberately **Tk-free and model-agnostic**: it stores callables
and never imports the edit model, so ``edit_model`` can import :class:`Command`
from here without an import cycle. The UI subscribes to the stack for enable/
disable of the toolbar buttons; view refresh is driven by the caller.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

# Consecutive commands sharing a coalesce_key merge into a single undo step if
# they arrive within this window (so a quick multi-step rotate is one Ctrl+Z).
COALESCE_WINDOW = 1.2


@dataclass
class Command:
    label_key: str                       # UNtranslated; localize at display via _()
    do: Callable[[], None]
    undo: Callable[[], None]
    coalesce_key: str | None = None
    _t: float = field(default=0.0, repr=False)


def _merge(old: Command, new: Command) -> Command:
    """Fold ``new`` (already executed) into ``old`` so one undo reverts both.

    do  = old.do then new.do   (re-applies the whole run on redo)
    undo = new.undo then old.undo (reverts back to before the run started)
    """
    def do():
        old.do()
        new.do()

    def undo():
        new.undo()
        old.undo()

    return Command(new.label_key, do, undo, new.coalesce_key, new._t)


class UndoStack:
    def __init__(self, limit: int = 100):
        self._undo: list[Command] = []
        self._redo: list[Command] = []
        self._limit = limit
        self._subs: list[Callable[[], None]] = []

    # --- mutation ---------------------------------------------------------
    def push(self, cmd: Command, *, execute: bool = True) -> None:
        """Run ``cmd.do()`` (unless already applied), record it, clear redo."""
        cmd._t = time.time()
        if execute:
            cmd.do()
        top = self._undo[-1] if self._undo else None
        if (top is not None and cmd.coalesce_key is not None
                and top.coalesce_key == cmd.coalesce_key
                and cmd._t - top._t <= COALESCE_WINDOW):
            self._undo[-1] = _merge(top, cmd)
        else:
            self._undo.append(cmd)
            while len(self._undo) > self._limit:
                self._undo.pop(0)
        self._redo.clear()
        self._notify()

    def undo(self) -> None:
        if not self._undo:
            return
        cmd = self._undo.pop()
        cmd.undo()
        self._redo.append(cmd)
        self._notify()

    def redo(self) -> None:
        if not self._redo:
            return
        cmd = self._redo.pop()
        cmd.do()
        self._undo.append(cmd)
        self._notify()

    def clear(self) -> None:
        self._undo.clear()
        self._redo.clear()
        self._notify()

    # --- queries ----------------------------------------------------------
    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def peek_undo_label(self) -> str | None:
        return self._undo[-1].label_key if self._undo else None

    def peek_redo_label(self) -> str | None:
        return self._redo[-1].label_key if self._redo else None

    # --- observers --------------------------------------------------------
    def subscribe(self, cb: Callable[[], None]) -> None:
        self._subs.append(cb)

    def _notify(self) -> None:
        for cb in self._subs:
            try:
                cb()
            except Exception:
                pass
