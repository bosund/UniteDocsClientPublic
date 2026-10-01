"""Power-sortering: sorter hele filer efter oprettelse, PDF-dato eller en dato
fundet i filnavnet.

Fladen er en liste med een raekke pr. fil. Klik paa en kolonneoverskrift
sorterer, klik igen vender retningen. Listen EJER ikke raekkefoelgen -- den er
et forslag; foerst "Anvend raekkefoelge" skubber den ind i ``EditModel`` som eet
undo-trin (``MainWindow._open_power_sort``).

Datoen i filnavnet findes af ``filename_dates`` -- automatisk paa tvaers af
alle filerne, eller ud fra de tegn brugeren markerer i ``ManualDateDialog``.
"""

from __future__ import annotations

import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHeaderView, QLabel,
                               QListWidget, QSizePolicy,
                               QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from . import filename_dates as fdates
from . import icons_vector, qt_util, theme
from .localization import LocalizationManager
from .qt_util import button_row, muted_label

_ = LocalizationManager.get_text

_SORT_ROLE = Qt.ItemDataRole.UserRole + 1
_IID_ROLE = Qt.ItemDataRole.UserRole


def _fmt_date(d: "datetime.date | None") -> str:
    if d is None:
        return ""
    return _("%(day)02d/%(month)02d-%(year)04d") % {
        "day": d.day, "month": d.month, "year": d.year}


def _fmt_stamp(stamp: str) -> str:
    """``"YYYY-MM-DD HH:MM"`` -> ``"31/06-2022 14:05"``. Tom hvis ukendt."""
    if not stamp:
        return ""
    try:
        d = datetime.datetime.strptime(stamp, "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    return "%s %02d:%02d" % (_fmt_date(d.date()), d.hour, d.minute)


def pattern_text(tokens) -> str:
    """Formatet som laesbar skabelon, fx ``dd-mm-åååå`` / ``dd måned åååå``.
    Bogstaverne oversaettes: aar er "yyyy" paa engelsk, "jjjj" paa tysk."""
    letters = {"d": _("d"), "m": _("m"), "y": _("å")}
    out = []
    for t in tokens:
        if isinstance(t, str):
            out.append(t)
        elif t[0] == "m" and t[1] == 0:
            out.append(_("måned"))
        else:
            out.append(letters[t[0]] * t[1])
    return "".join(out)


class _Row(QTreeWidgetItem):
    """Sorterer paa ``_SORT_ROLE``, og tomme felter staar SIDST i begge
    retninger -- en fil uden dato skal ikke lede listen naar man vender den."""

    def __lt__(self, other):
        tree = self.treeWidget()
        col = tree.sortColumn() if tree is not None else 0
        a, b = self.data(col, _SORT_ROLE), other.data(col, _SORT_ROLE)
        a_empty, b_empty = a in (None, ""), b in (None, "")
        if a_empty != b_empty:
            desc = (tree is not None and tree.header().sortIndicatorOrder()
                    == Qt.SortOrder.DescendingOrder)
            return (not a_empty) != desc
        if a_empty or a == b:
            # Stabil tiebreak: filnavn, derefter den oprindelige plads.
            return ((self.data(0, _SORT_ROLE), self.data(1, _IID_ROLE))
                    < (other.data(0, _SORT_ROLE), other.data(1, _IID_ROLE)))
        return a < b


class PowerSortDialog(QDialog):
    """``run()`` giver ``(iid-raekkefoelge, {iid: "YYYY-MM-DD"} | None)``, eller
    ``None`` ved annullering. Datoerne er ``None`` hvis der ikke er ledt efter
    datoer i filnavnene i denne omgang."""

    COL_NAME, COL_CREATED, COL_PDF, COL_FNAME = range(4)

    def __init__(self, parent, rows):
        """``rows`` er ``[(iid, sti, name_date)]`` i modellens nuvaerende
        raekkefoelge; ``name_date`` er en tidligere fundet dato eller ""."""
        super().__init__(parent)
        self.setWindowTitle(_("Power-sortering"))
        self.setModal(True)
        self.resize(860, 560)
        self._result = None
        self._items: dict = {}
        self._stems: dict = {}
        self._found = False              # er der ledt efter datoer i denne omgang?

        lay = QVBoxLayout(self)
        lay.setContentsMargins(18, 16, 18, 12)
        lay.setSpacing(theme.SPACE["md"])

        intro = QLabel(_("Klik på en kolonneoverskrift for at sortere, og igen for "
                         "at vende rækkefølgen. Intet ændres før du trykker "
                         "Anvend rækkefølge."), self)
        intro.setWordWrap(True)
        lay.addWidget(intro)

        self._auto_btn = cmd_button(self, "sort", _("Find dato i filnavn"),
                                    self._auto_detect)
        self._manual_btn = cmd_button(self, "tool_select", _("Manuel…"), self._manual)
        tools = button_row(self, self._auto_btn, self._manual_btn, align_right=False)
        tools.layout().addStretch(1)
        lay.addWidget(tools)
        self._status = muted_label("", parent=self)
        self._status.setWordWrap(True)
        lay.addWidget(self._status)

        tree = QTreeWidget(self)
        tree.setColumnCount(4)
        qt_util.pad_tree(tree)
        tree.setHeaderLabels([_("Filnavn"), _("Oprettet"), _("PDF-dato"),
                              _("Dato i filnavn")])
        tree.setRootIsDecorated(False)
        tree.setItemsExpandable(False)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        head = tree.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(self.COL_NAME, QHeaderView.ResizeMode.Stretch)
        for col in (self.COL_CREATED, self.COL_PDF, self.COL_FNAME):
            head.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        for i, (iid, path, name_date) in enumerate(rows):
            name = Path(path).name
            try:
                prev = datetime.date.fromisoformat(name_date) if name_date else None
            except ValueError:
                prev = None
            it = _Row([name, "…", "…", _fmt_date(prev)])
            it.setData(self.COL_FNAME, _SORT_ROLE, prev.isoformat() if prev else "")
            it.setData(self.COL_NAME, _SORT_ROLE, name.casefold())
            it.setData(self.COL_NAME, _IID_ROLE, iid)
            it.setData(1, _IID_ROLE, i)                   # oprindelig plads
            it.setToolTip(self.COL_NAME, path)
            tree.addTopLevelItem(it)
            self._items[iid] = it
            self._stems[iid] = Path(path).stem
        # Ingen sortering foer brugeren beder om det: listen starter i den
        # raekkefoelge filerne har nu.
        head.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        tree.setSortingEnabled(True)
        head.setSortIndicatorShown(True)
        self._tree = tree
        lay.addWidget(tree, 1)

        cancel = cmd_button(self, "cancel_preview", _("Annuller"), self.reject)
        apply_btn = cmd_button(self, "ok", _("Anvend rækkefølge"), self._apply,
                               primary=True)
        lay.addWidget(button_row(self, cancel, apply_btn))

    # --- data ----------------------------------------------------------
    def set_dates(self, iid: str, dates: dict) -> None:
        """Kaldes via ``app._queue`` naar workeren har laest en fil."""
        it = self._items.get(iid)
        if it is None:
            return
        for col, key in ((self.COL_CREATED, "created"), (self.COL_PDF, "pdf_date")):
            stamp = dates.get(key) or ""
            it.setText(col, _fmt_stamp(stamp))
            it.setData(col, _SORT_ROLE, stamp)

    def _set_filename_dates(self, rule) -> int:
        self._found = True
        hits = 0
        tree = self._tree
        tree.setSortingEnabled(False)              # ellers flytter hver raekke sig
        for iid, it in self._items.items():
            d = rule.parse(self._stems[iid])
            hits += d is not None
            it.setText(self.COL_FNAME, _fmt_date(d) if d else "—")
            it.setData(self.COL_FNAME, _SORT_ROLE, d.isoformat() if d else "")
        tree.setSortingEnabled(True)
        tree.sortByColumn(self.COL_FNAME, Qt.SortOrder.AscendingOrder)
        return hits

    def _auto_detect(self) -> None:
        found = fdates.detect_rule(self._stems.values())
        if found is None:
            self._status.setText(_("Der blev ikke fundet en dato i filnavnene. "
                                   "Prøv Manuel…"))
            return
        hits = self._set_filename_dates(found.rule)
        self._status.setText(
            _("Mønster: %(pattern)s — dato fundet i %(hits)d af %(total)d filer")
            % {"pattern": pattern_text(found.rule.tokens), "hits": hits,
               "total": len(self._items)})

    def _manual(self) -> None:
        stems = list(dict.fromkeys(self._stems.values()))[:10]
        rule = ManualDateDialog(self, stems).run()
        if rule is None:
            return
        hits = self._set_filename_dates(rule)
        self._status.setText(
            _("Mønster: %(pattern)s — dato fundet i %(hits)d af %(total)d filer")
            % {"pattern": pattern_text(rule.tokens), "hits": hits,
               "total": len(self._items)})

    # --- resultat ------------------------------------------------------
    def _apply(self) -> None:
        items = [self._tree.topLevelItem(i)
                 for i in range(self._tree.topLevelItemCount())]
        order = [it.data(self.COL_NAME, _IID_ROLE) for it in items]
        dates = ({it.data(self.COL_NAME, _IID_ROLE):
                  it.data(self.COL_FNAME, _SORT_ROLE) or "" for it in items}
                 if self._found else None)
        self._result = (order, dates)
        self.accept()

    def run(self):
        self.exec()
        return self._result


def cmd_button(parent, icon: str, text: str, slot, *, primary: bool = False
               ) -> QToolButton:
    """Samme store knap som kommandobaren: ikon over tekst, primaer i accent.

    ``MainWindow._cmd_button`` gentegner ikonet ved temaskift; dialogen her er
    modal og kortlivet, saa ikonet tegnes een gang ved oprettelse."""
    btn = QToolButton(parent)
    btn.setObjectName("CmdPrimary" if primary else "CmdButton")
    btn.setText(text)
    btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
    size = theme.ICON["cmdlg"]
    btn.setIcon(icons_vector.qicon(
        icon, size, theme.C["selection_fg"] if primary else None))
    btn.setIconSize(QSize(size, size))
    btn.setMinimumWidth(76)
    btn.setCursor(Qt.CursorShape.PointingHandCursor)
    btn.clicked.connect(slot)
    return btn


class MarkableName(QWidget):
    """Et filnavn med stor skrift, hvor brugeren markerer et tegnomraade.

    Tegnes selv i stedet for at vaere en ``QLineEdit``: de felter der allerede
    er markeret (aar, maaned, dag) skal blive staaende i hver sin farve med en
    etiket under, mens det naeste markeres -- det kan et tekstfelt ikke vise.
    Traek med musen, eller dobbeltklik for at tage et helt tal/ord.
    """

    selection_changed = Signal()

    _PAD = 14
    _MAX_PT, _MIN_PT = 22.0, 11.0
    # Luft mellem tegnene: "31052022" skal kunne rammes tegn for tegn med
    # musen, og to naborammer (maaned | aar) maa ikke flyde sammen.
    _TRACK = 4.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.CursorShape.IBeamCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._text = ""
        self._anchor = self._pos = 0
        self.marks: dict = {}          # felt -> (start, slut)
        self.styles: dict = {}         # felt -> (tokennavn, etiket)
        self._font = theme.font("display")
        self._font.setBold(False)
        self._xs: list = [float(self._PAD)]
        self._relayout()

    def set_text(self, text: str) -> None:
        self._text = text
        self._anchor = self._pos = 0
        self._relayout()

    def selection(self):
        a, b = sorted((self._anchor, self._pos))
        return (a, b) if b > a else None

    def set_selection(self, a: int, b: int) -> None:
        self._anchor, self._pos = a, b
        self.update()
        self.selection_changed.emit()

    def clear_selection(self) -> None:
        self.set_selection(0, 0)

    # --- geometri --------------------------------------------------------
    def _relayout(self) -> None:
        """Stoerst mulige skrift der faar HELE navnet paa een linje."""
        avail = max(50, self.width() - 2 * self._PAD)
        f = QFont(self._font)
        pt = self._MAX_PT
        while True:
            f.setPointSizeF(pt)
            if (QFontMetricsF(f).horizontalAdvance(self._text)
                    + self._TRACK * len(self._text) <= avail or pt <= self._MIN_PT):
                break
            pt -= 1
        self._font = f
        fm = QFontMetricsF(f)
        self._xs = [self._PAD + fm.horizontalAdvance(self._text[:i]) + self._TRACK * i
                    for i in range(len(self._text) + 1)]
        small = QFontMetricsF(theme.font("small"))
        self.setFixedHeight(int(QFontMetricsF(self._font_at(self._MAX_PT)).height()
                                + small.height() + 3 * self._PAD))
        self.update()

    def _font_at(self, pt: float) -> QFont:
        f = QFont(self._font)
        f.setPointSizeF(pt)
        return f

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._relayout()

    def _hit(self, x: float) -> int:
        xs = self._xs
        return min(range(len(xs)), key=lambda i: abs(xs[i] - x))

    def _text_rect(self) -> QRectF:
        fm = QFontMetricsF(self._font)
        # Lodret centreret i felterne over etiketlinjen.
        small_h = QFontMetricsF(theme.font("small")).height()
        top = (self.height() - small_h - fm.height()) / 2 - 2
        return QRectF(0, top, self.width(), fm.height())

    # --- mus -------------------------------------------------------------
    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._anchor = self._pos = self._hit(e.position().x())
            self.update()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton:
            self._pos = self._hit(e.position().x())
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.selection_changed.emit()

    def mouseDoubleClickEvent(self, e):
        """Tag hele loebet af cifre -- eller bogstaver -- under markoeren."""
        t = self._text
        if not t:
            return
        i = min(max(self._hit(e.position().x()), 0), len(t) - 1)
        if not t[i].isalnum() and i > 0 and t[i - 1].isalnum():
            i -= 1
        if not t[i].isalnum():
            return
        kind = str.isdigit if t[i].isdigit() else str.isalpha
        a = i
        while a > 0 and kind(t[a - 1]):
            a -= 1
        b = i + 1
        while b < len(t) and kind(t[b]):
            b += 1
        self.set_selection(a, b)

    # --- tegning ---------------------------------------------------------
    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        frame = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(theme.qc("border_strong"), 1))
        p.setBrush(theme.qc("surface"))
        p.drawRoundedRect(frame, 6, 6)

        tr = self._text_rect()
        small = theme.font("small")
        small_h = QFontMetricsF(small).height()
        for key, (a, b) in self.marks.items():
            token, label = self.styles.get(key, ("accent", ""))
            col = theme.qc(token)
            # Rammen holder sig inden for sine egne tegn (+ halvdelen af
            # luften), saa to naborammer aldrig overlapper.
            half = self._TRACK / 2
            box = QRectF(self._xs[a] - half + 0.75, tr.top() - 2,
                         self._xs[b] - self._xs[a] - 1.5, tr.height() + 4)
            fill = QColor(col)
            fill.setAlpha(45)
            p.setPen(QPen(col, 1.5))
            p.setBrush(fill)
            p.drawRoundedRect(box, 4, 4)
            p.setFont(small)
            p.setPen(col)
            p.drawText(QRectF(box.left() - 40, box.bottom() + 3,
                              box.width() + 80, small_h),
                       Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop,
                       label)

        sel = self.selection()
        if sel:
            a, b = sel
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(theme.qc("selection"))
            half = self._TRACK / 2
            p.drawRoundedRect(QRectF(self._xs[a] - half, tr.top(),
                                     self._xs[b] - self._xs[a], tr.height()), 3, 3)

        p.setFont(self._font)
        base = tr.top() + QFontMetricsF(self._font).ascent()
        for i, ch in enumerate(self._text):
            inside = sel is not None and sel[0] <= i < sel[1]
            p.setPen(theme.qc("selection_fg" if inside else "text"))
            p.drawText(QPointF(self._xs[i], base), ch)
        p.end()


class ManualDateDialog(QDialog):
    """Guide: vaelg et filnavn, marker aar/maaned/dag i det, kontroller.

    Eet trin pr. side. Det valgte navn staar med stor skrift oeverst,
    spoergsmaalet under det, og knapperne under det igen -- i kommandobarens
    store format. Alle markeringer ligger i SAMME navn, for reglen bygges af
    tegnene mellem dem (``filename_dates.rule_from_marks``). ``run()`` giver en
    ``DateRule`` eller ``None``.
    """

    _FIELDS = ("y", "m", "d")
    _TOKENS = {"y": "accent", "m": "success", "d": "warning"}
    # Side 0 = vaelg fil, 1-3 = aar/maaned/dag, 4 = kontroller.
    _PAGE_CHOOSE, _PAGE_CHECK = 0, 4

    @staticmethod
    def _prompt_for(key: str) -> str:
        # Bogstavelige _()-kald: Babel kan ikke udtraekke _(variabel).
        return {"y": _("Marker de tegn der repræsenterer årstallet"),
                "m": _("Marker de tegn der repræsenterer måneden"),
                "d": _("Marker de tegn der repræsenterer dagen")}[key]

    @staticmethod
    def _label_for(key: str) -> str:
        return {"y": _("År"), "m": _("Måned"), "d": _("Dag")}[key]

    @staticmethod
    def _error_for(code: str) -> str:
        return {"missing": _("Marker både årstal og måned."),
                "empty": _("En markering er tom."),
                "overlap": _("Markeringerne overlapper hinanden."),
                "year": _("Årstallet skal være 2 eller 4 cifre."),
                "month": _("Måneden skal være 1-2 cifre eller et månedsnavn."),
                "day": _("Dagen skal være 1-2 cifre."),
                }.get(code, _("Markeringen kan ikke bruges."))

    def __init__(self, parent, names):
        super().__init__(parent)
        self.setWindowTitle(_("Manuel dato i filnavn"))
        self.setModal(True)
        self.resize(760, 480)
        self.setMinimumWidth(560)
        self._names = list(names)
        self._name = ""
        self._marks: dict = {}
        self._rule = None
        self._result = None
        self._page = self._PAGE_CHOOSE

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 18, 22, 16)
        lay.setSpacing(theme.SPACE["lg"])

        self._step_lbl = muted_label("", parent=self)
        lay.addWidget(self._step_lbl)

        # Det valgte filnavn -- stort, oeverst, paa alle markeringstrin.
        self._marker = MarkableName(self)
        for key in self._FIELDS:
            self._marker.styles[key] = (self._TOKENS[key], self._label_for(key))
        self._marker.selection_changed.connect(self._update_buttons)
        lay.addWidget(self._marker)

        self._prompt = QLabel(self)
        self._prompt.setFont(theme.font("title"))
        self._prompt.setWordWrap(True)
        lay.addWidget(self._prompt)
        self._hint = muted_label("", parent=self)
        self._hint.setWordWrap(True)
        lay.addWidget(self._hint)

        # Side 0: listen der vaelges fra. Sidste side: de fundne datoer.
        self._list = QListWidget(self)
        self._list.addItems(self._names)
        self._list.setFont(theme.font("lead"))
        self._list.setSpacing(2)
        self._list.itemSelectionChanged.connect(self._update_buttons)
        self._list.itemDoubleClicked.connect(lambda _i: self._go_next())
        lay.addWidget(self._list, 1)

        self._check = QTreeWidget(self)
        self._check.setColumnCount(2)
        qt_util.pad_tree(self._check)
        self._check.setHeaderLabels([_("Filnavn"), _("Dato i filnavn")])
        self._check.setRootIsDecorated(False)
        self._check.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._check.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        head = self._check.header()
        head.setStretchLastSection(False)
        head.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        head.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        lay.addWidget(self._check, 1)

        # Knapperne staar lige under spoergsmaalet paa markeringstrinene; paa
        # liste-/kontrolsiden skubbes de ned under listen.
        self._back = cmd_button(self, "back", _("Tilbage"), self._go_back)
        self._skip = cmd_button(self, "cancel_preview", _("Ingen dag"), self._skip_day)
        self._next = cmd_button(self, "next", _("Næste"), self._go_next, primary=True)
        self._apply = cmd_button(self, "ok", _("Anvend"), self._go_next, primary=True)
        cancel = cmd_button(self, "cancel_preview", _("Annuller"), self.reject)
        nav = button_row(self, self._back, self._skip, self._next, self._apply,
                         align_right=False)
        nav.layout().addStretch(1)
        nav.layout().addWidget(cancel)
        lay.addWidget(nav)
        self._show_page()

    # --- sider -----------------------------------------------------------
    def _field(self) -> str | None:
        return self._FIELDS[self._page - 1] if 1 <= self._page <= 3 else None

    def _show_page(self) -> None:
        page, field_ = self._page, self._field()
        self._step_lbl.setText(_("Trin %(step)d af %(total)d") % {
            "step": page + 1, "total": self._PAGE_CHECK + 1})
        self._marker.setVisible(page != self._PAGE_CHOOSE)
        self._list.setVisible(page == self._PAGE_CHOOSE)
        self._check.setVisible(page == self._PAGE_CHECK)
        self._back.setVisible(page > self._PAGE_CHOOSE)
        self._skip.setVisible(field_ == "d")
        self._next.setVisible(page != self._PAGE_CHECK)
        self._apply.setVisible(page == self._PAGE_CHECK)
        if page == self._PAGE_CHOOSE:
            self._prompt.setText(_("Vælg en fil"))
            self._hint.setText(_("Vælg det filnavn hvor datoen er nemmest at se."))
        elif field_ is not None:
            self._prompt.setText(self._prompt_for(field_))
            self._hint.setText(_("Træk med musen hen over tegnene, eller "
                                 "dobbeltklik på et tal eller et ord."))
        else:
            self._prompt.setText(_("Kontrollér datoerne"))
            self._hint.setText(_("Sådan læses datoen i filnavnene. Passer den "
                                 "ikke, så gå tilbage og ret markeringen."))
        # Felterne der er markeret FOER dette trin staar i farve; det der
        # markeres nu, er den blaa markering.
        self._marker.marks = {k: v for k, v in self._marks.items()
                              if field_ is None or k != field_}
        if field_ in self._marks:
            self._marker.set_selection(*self._marks[field_])
        else:
            self._marker.clear_selection()
        if field_ is not None:
            self._marker.setFocus()
        self._update_buttons()
        # Markeringstrinene har ingen liste -- saa skal vinduet heller ikke
        # have et tomt felt under knapperne.
        self.layout().activate()
        self.resize(self.width(), 480 if field_ is None
                    else self.layout().sizeHint().height())

    def _update_buttons(self) -> None:
        if self._page == self._PAGE_CHOOSE:
            ok = bool(self._list.selectedItems())
        elif self._field() is not None:
            ok = self._marker.selection() is not None
        else:
            ok = self._rule is not None
        self._next.setEnabled(ok)
        self._apply.setEnabled(ok)

    def _go_next(self) -> None:
        if self._page == self._PAGE_CHECK:
            if self._rule is not None:
                self._result = self._rule
                self.accept()
            return
        if not self._next.isEnabled():
            return
        if self._page == self._PAGE_CHOOSE:
            name = self._list.selectedItems()[0].text()
            if name != self._name:
                self._name, self._marks = name, {}
                self._marker.set_text(name)
            self._page = 1
        else:
            field_ = self._field()
            span = self._marker.selection()
            problem = self._check_mark(field_, span)
            if problem:
                qt_util.warn(self, _("Manuel dato i filnavn"), self._error_for(problem))
                return
            self._marks[field_] = span
            if field_ == "d" and not self._build():
                return
            self._page += 1
        self._show_page()

    def _skip_day(self) -> None:
        self._marks.pop("d", None)
        if self._build():
            self._page = self._PAGE_CHECK
            self._show_page()

    def _go_back(self) -> None:
        self._page -= 1
        self._rule = None
        self._show_page()

    def _check_mark(self, field_: str, span) -> str | None:
        """Fang den aabenlyse fejl paa det trin hvor den sker, ikke til sidst."""
        a, b = span
        text = self._name[a:b]
        for k, (x, y) in self._marks.items():
            if k != field_ and a < y and x < b:
                return "overlap"
        if field_ == "y":
            return None if text.isdigit() and len(text) in (2, 4) else "year"
        if text.isdigit():
            return None if len(text) <= 2 else ("month" if field_ == "m" else "day")
        if field_ == "m" and fdates.month_number(text) is not None:
            return None
        return "month" if field_ == "m" else "day"

    def _build(self) -> bool:
        try:
            rule = fdates.rule_from_marks(self._name, self._marks)
        except fdates.MarkError as e:
            qt_util.warn(self, _("Manuel dato i filnavn"), self._error_for(e.code))
            return False
        self._rule = rule
        self._check.clear()
        for name in self._names:
            d = rule.parse(name)
            it = QTreeWidgetItem([name, _fmt_date(d) if d else "—"])
            if d is None:
                it.setForeground(1, theme.qc("text_muted"))
            self._check.addTopLevelItem(it)
        return True

    def run(self):
        self.exec()
        return self._result
