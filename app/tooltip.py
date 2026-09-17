"""Tooltips.

Qt har allerede et tooltip-vindue der foelger systemets tema, forsvinder af sig
selv og aldrig staeler fokus -- 8.x' egen ``Toplevel``-implementering med
``overrideredirect``, ``-topmost``, ``-disabled`` og manuel skaermkant-klemning
er derfor vaek. Tilbage staar den ene regel den bar:

**Genvejshint sendes via ``shortcut=`` -- aldrig konkateneret ind i den
oversatte streng.** ``Ctrl+Z`` skal ikke oversaettes, og en oversaetter skal
ikke kunne komme til at flytte det ind i saetningen. Her saettes genvejen derfor
paa sin egen linje i en daempet tone, sammensat *efter* oversaettelsen.

``text`` maa vaere en ``str`` eller et ``callable() -> str``. Callable-formen
gør at ``_()`` genevalueres hver gang tooltippet vises, saa teksten overlever et
sprogskift uden at nogen skal huske at opdatere den.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtWidgets import QWidget

from . import theme
from .logging_config import get_logger

logger = get_logger(__name__)


def _compose(text: str, shortcut: str | None) -> str:
    """Byg tooltip-teksten. Genvejen faar sin egen daempede linje."""
    body = (text or "").strip()
    if not shortcut:
        return body
    # Rich text, saa genvejen kan daempes uden at roere den oversatte streng.
    esc = (body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return ('<div style="white-space:pre">%s<br/>'
            '<span style="color:%s">%s</span></div>'
            % (esc, theme.C["text_muted"], shortcut))


class _DynamicTip(QObject):
    """Genevaluerer en callable tooltip-tekst hver gang den skal vises.

    Qt spoerger widget'en om dens tooltip via ``QEvent.ToolTip``; ved at
    opdatere teksten i det oejeblik faar vi den friske oversaettelse med, uden
    at holde en liste over alle tooltips ved sprogskift.
    """

    def __init__(self, widget: QWidget, provider, shortcut: str | None):
        super().__init__(widget)
        self._provider = provider
        self._shortcut = shortcut
        widget.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt-API
        if event.type() == QEvent.Type.ToolTip:
            try:
                obj.setToolTip(_compose(self._provider(), self._shortcut))
            except Exception as e:
                logger.debug("Dynamisk tooltip fejlede: %s", e)
        return False


class Tooltip:
    """Bevaret navn, saa kaldesteder laeser som foer."""

    @staticmethod
    def attach(widget: QWidget, text, *, shortcut: str | None = None,
               delay: int | None = None) -> None:
        """Haeng en tooltip paa ``widget``.

        ``delay`` findes for API-kompatibilitet; Qt styrer selv forsinkelsen
        globalt (``theme.TOOLTIP_DELAY_MS`` saettes i ``apply_theme``-kaldet).
        """
        if callable(text):
            _DynamicTip(widget, text, shortcut)
            try:
                widget.setToolTip(_compose(text(), shortcut))
            except Exception as e:
                logger.debug("Tooltip-tekst kunne ikke hentes: %s", e)
            return
        widget.setToolTip(_compose(str(text), shortcut))

    @staticmethod
    def hide_all() -> None:
        """Skjul et evt. synligt tooltip. Findes fordi 8.x kaldte den foer en
        UI-nedrivning; Qt rydder selv op, saa den er en no-op i dag."""
        from PySide6.QtWidgets import QToolTip
        QToolTip.hideText()
