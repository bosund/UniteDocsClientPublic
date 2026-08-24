"""Unite Docs client-pakke.

Versionsnummeret har ÉN kilde: ``client/__version__.py``. Compileren
(``compiler/kompilerv3.py``) laeser den fil direkte via regex, og entry-pointet
(``unitedocs.py``) importerer den, saa den bundtes i den frosne build. Her
re-eksporteres den blot, saa ``from app import __version__`` fortsat virker uden
et andet sted at vedligeholde tallet.
"""

try:
    from __version__ import __version__
except Exception:  # bør aldrig ske (entry-pointet importerer __version__ først)
    __version__ = "0.0.0"
