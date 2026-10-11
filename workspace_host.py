"""WorkspaceHost: the single main window that holds everything TORCA
used to open as separate top-level windows (Ham Dashboard, one window
per radio, the CW/APRS/SSTV/... tools, the Log Book).

The host shows 1-4 "workspaces" (WorkspacePane, a QTabWidget each); any
hosted window is a tab in exactly one workspace and can be moved to
another from the tab's right-click menu, or popped out into a floating
window and docked back.

Placement rule for a newly presented window (see present()): the first
EMPTY visible workspace, otherwise a new tab in the workspace the
operator last used.

Hosted windows are the app's existing top-level QWidgets, unchanged:
each is wrapped in a QScrollArea page (so a pane narrower than a
window's minimum size scrolls instead of forcing the whole host
bigger) and keeps its own closeEvent. Their "singleton" convention --
closeEvent ignores the close and hide()s itself, so the same instance
can be reopened (and the web remote's references to it stay valid) --
is detected here by watching for an explicit hide() of the hosted
widget itself, which is how the tab gets removed. A hosted window that
merely sits on a non-current tab is hidden through its scroll-area
page instead, so it is NOT mistaken for closed.

Qt-only, no radio/app imports -- the dashboard and radio windows reach
this through present_window() below, which falls back to the old
show()/raise_() behaviour when no host exists (tests, headless use).
"""

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from workspace_state import fits_grid, tree_from_json, tree_to_json
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

MAX_WORKSPACES = 4

# ---------------------------------------------------------------------------
# Workspace layout = a tree of splits (pure functions, no Qt).
#
# A leaf is a workspace index (0-3). A split is (orientation, children):
# orientation "h" puts children side by side, "v" stacks them. Every child of
# a split gets an EQUAL share of it, and nested splits of the same orientation
# are always flattened, so "A | B | C" is three equal columns, never 1/4, 1/4,
# 1/2. Workspace 1 (index 0) is always present.
# ---------------------------------------------------------------------------

H, V = "h", "v"
_ORIENTATION = {"right": H, "below": V}


def tree_leaves(tree):
    """Workspace indices in layout order."""
    if isinstance(tree, int):
        return [tree]
    return [leaf for child in tree[1] for leaf in tree_leaves(child)]


def tree_normalize(tree):
    """Flattens nested same-orientation splits and collapses single-child ones."""
    if isinstance(tree, int):
        return tree
    orientation, children = tree
    flat = []
    for child in children:
        child = tree_normalize(child)
        if isinstance(child, tuple) and child[0] == orientation:
            flat.extend(child[1])
        else:
            flat.append(child)
    return flat[0] if len(flat) == 1 else (orientation, tuple(flat))


def tree_add(tree, index, direction, scope, new_index):
    """The layout after adding workspace `new_index` to the 'right' of or
    'below' either just workspace `index` (scope "this") or the whole
    layout (scope "all"). The new workspace splits equally with whatever it
    ends up sharing a split with."""
    orientation = _ORIENTATION[direction]
    if scope == "all":
        return tree_normalize((orientation, (tree, new_index)))

    def grow(node):
        if isinstance(node, int):
            return (orientation, (node, new_index)) if node == index else node
        return (node[0], tuple(grow(child) for child in node[1]))

    return tree_normalize(grow(tree))


def tree_remove(tree, index):
    """The layout after removing workspace `index`, closing the gap."""
    def drop(node):
        if isinstance(node, int):
            return None if node == index else node
        kept = [child for child in (drop(c) for c in node[1]) if child is not None]
        return (node[0], tuple(kept)) if kept else None

    return tree_normalize(drop(tree))


def tree_for_count(n):
    """Standard arrangements used by set_workspace_count(): 1 = one, 2 = side
    by side, 3 = a row of two above one, 4 = two rows of two."""
    return [0, (H, (0, 1)), (V, ((H, (0, 1)), 2)), (V, ((H, (0, 1)), (H, (2, 3))))][min(max(n, 1), 4) - 1]

PLUS_BUTTON_STYLE = (
    "QToolButton { font-weight: bold; font-size: 16px; padding: 0 8px; }"
    "QToolButton::menu-indicator { image: none; }"
)

_TAB_TITLE_MAX = 34

_HOST = None  # the live WorkspaceHost, if any -- see present_window()


def present_window(widget):
    """Shows and focuses `widget`, wherever it lives: inside the host
    (selecting its tab, or placing it per the placement rule) when a
    WorkspaceHost exists, else as a plain top-level window."""
    if _HOST is not None:
        _HOST.present(widget)
        return
    widget.show()
    widget.raise_()
    widget.activateWindow()


def retire_button(parent, button):
    """Takes a button out of the visible UI while keeping the object.
    Its entry now lives in a workspace's "+" menu, but the button still
    carries the state the rest of the code reads and writes (enabled,
    checked, progress text, clicked/toggled signals) -- so the menu
    mirrors it and calls click() rather than every one of those code
    paths being rewritten."""
    button.setParent(parent)
    button.hide()


def _short_title(title):
    return title if len(title) <= _TAB_TITLE_MAX else title[: _TAB_TITLE_MAX - 1] + "…"


class _Entry:
    """Bookkeeping for one hosted window."""

    def __init__(self, widget, closable):
        self.widget = widget
        self.closable = closable
        self.page = None       # the QScrollArea wrapping it while docked
        self.pane = None       # the WorkspacePane holding that page while docked
        self.floating = False  # popped out into its own top-level window


class _AddTabBar(QTabBar):
    """A tab bar with a "+" button that sits immediately after the last
    tab (Qt has no built-in "new tab" button, and a corner widget can
    only be pinned to the far edge of the bar). The button is a child of
    the bar, repositioned whenever the tabs are laid out or the bar is
    resized.

    When there are so many tabs that the bar scrolls, there is no free
    spot inside the bar for it (the tabs fill everything up to Qt's
    scroll arrows), so the in-bar button hides and overflowChanged(True)
    tells the pane to show its corner "+" instead."""

    overflowChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._overflowing = False
        self.add_button = QToolButton(self)
        self.add_button.setText("+")
        self.add_button.setToolTip("Add a tab to this workspace")
        self.add_button.setAutoRaise(True)
        self.add_button.setPopupMode(QToolButton.InstantPopup)
        self.add_button.setStyleSheet(PLUS_BUTTON_STYLE)

    def reposition_add_button(self):
        button = self.add_button
        button.adjustSize()
        count = self.count()
        last_right = self.tabRect(count - 1).right() + 1 if count else 0
        x = last_right + 2
        overflowing = x + button.width() > self.width()
        if overflowing != self._overflowing:
            self._overflowing = overflowing
            self.overflowChanged.emit(overflowing)
        button.setVisible(not overflowing)
        if not overflowing:
            button.move(x, max(0, (self.height() - button.height()) // 2))
            button.raise_()

    # A tab bar with no tabs reports zero height, and QTabWidget sizes the
    # bar (and the corner label beside it) from the size hint -- so an
    # empty workspace would lose its "Workspace N" menu button.
    _MIN_BAR_HEIGHT = 28

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(hint.width(), max(hint.height(), self._MIN_BAR_HEIGHT))

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(hint.width(), max(hint.height(), self._MIN_BAR_HEIGHT))

    def tabLayoutChange(self):
        super().tabLayoutChange()
        self.reposition_add_button()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.reposition_add_button()

    def showEvent(self, event):
        super().showEvent(event)
        self.reposition_add_button()


class WorkspacePane(QTabWidget):
    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.setTabBar(_AddTabBar(self))
        self.setDocumentMode(True)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setElideMode(Qt.ElideRight)
        self.setUsesScrollButtons(True)
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)

        self._menu_builder = None  # set by the host: fills a QMenu with what can be opened here

        # "Workspace N" doubles as the menu for adding/closing workspaces.
        self._badge = QToolButton()
        self._badge.setAutoRaise(True)
        self._badge.setPopupMode(QToolButton.InstantPopup)
        self._badge.setToolTip("Add or close workspaces")
        self._badge.setMenu(QMenu(self._badge))
        self._badge.menu().aboutToShow.connect(self._fill_workspace_menu)
        self._workspace_menu_builder = None

        # The "+" normally lives in the tab bar, right after the last tab.
        # A second one in the corner (beside the workspace label) takes
        # over only while the tabs overflow and the bar has no room for it.
        self._corner_add_button = QToolButton()
        self._corner_add_button.setText("+")
        self._corner_add_button.setToolTip("Add a tab to this workspace")
        self._corner_add_button.setAutoRaise(True)
        self._corner_add_button.setStyleSheet(PLUS_BUTTON_STYLE)
        self._corner_add_button.hide()
        for button in (self.tabBar().add_button, self._corner_add_button):
            button.setPopupMode(QToolButton.InstantPopup)
            button.setMenu(QMenu(button))
            button.menu().aboutToShow.connect(lambda b=button: self._fill_menu(b.menu()))
        self.tabBar().overflowChanged.connect(self._corner_add_button.setVisible)
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.setSpacing(0)
        corner_layout.addWidget(self._corner_add_button)
        corner_layout.addWidget(self._badge)
        self.setCornerWidget(corner, Qt.TopRightCorner)

        # An empty workspace shows no tab bar (so no "+" above), so it
        # gets its own button in the middle of the placeholder text.
        self._empty_button = QPushButton("+ Add Tab", self)
        self._empty_button.setMenu(QMenu(self._empty_button))
        self._empty_button.menu().aboutToShow.connect(lambda: self._fill_menu(self._empty_button.menu()))
        self.set_active(False)
        self._update_empty_state()

    def set_menu_builder(self, builder):
        self._menu_builder = builder

    def set_workspace_menu_builder(self, builder):
        self._workspace_menu_builder = builder

    def _fill_workspace_menu(self):
        menu = self._badge.menu()
        menu.clear()
        menu.setToolTipsVisible(True)
        if self._workspace_menu_builder is not None:
            self._workspace_menu_builder(menu)
        if menu.isEmpty():
            menu.addAction("(nothing to change)").setEnabled(False)

    def _fill_menu(self, menu):
        menu.clear()
        menu.setToolTipsVisible(True)
        if self._menu_builder is not None:
            self._menu_builder(menu)
        if menu.isEmpty():
            menu.addAction("(nothing to add)").setEnabled(False)

    def _update_empty_state(self):
        empty = self.count() == 0
        self._empty_button.setVisible(empty)
        if empty:
            self._position_empty_button()

    def _position_empty_button(self):
        size = self._empty_button.sizeHint()
        self._empty_button.move((self.width() - size.width()) // 2, self.height() // 2 + 60)

    def tabInserted(self, index):
        super().tabInserted(index)
        self._update_empty_state()

    def tabRemoved(self, index):
        super().tabRemoved(index)
        self._update_empty_state()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self.count() == 0:
            self._position_empty_button()

    def set_active(self, active):
        self._badge.setText(f"Workspace {self.index + 1}")
        self._badge.setStyleSheet(
            "QToolButton { color: %s; %s padding: 0 8px; }" % (
                ("#e8a33d", "font-weight: bold;") if active else ("#888", "")
            )
        )

    def paintEvent(self, event):
        super().paintEvent(event)
        if self.count() == 0:
            painter = QPainter(self)
            painter.setPen(Qt.gray)
            painter.drawText(
                self.rect().adjusted(0, 0, 0, -40), Qt.AlignCenter,
                f"Workspace {self.index + 1} — empty\n\nNew radios open here.\n"
                "Use + to add a tab, or right-click a tab in another workspace to move it here.",
            )


class _HideWatcher(QObject):
    """Event filter on a hosted widget: an explicit hide() of the widget
    itself (what a window's closeEvent does, or a completed close) means
    "the operator closed it" -- ask the host to drop its tab."""

    def __init__(self, host):
        super().__init__(host)
        self._host = host

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Hide and obj.isHidden():
            self._host._widget_hidden(obj)
        return False


class _MenuContext:
    """Handed to menu providers while the "+" menu of one workspace is
    being filled: open(widget) docks (or moves) the widget into THAT
    workspace; label() decorates a title with where the widget is
    already open, if anywhere."""

    def __init__(self, host, pane):
        self._host = host
        self._pane = pane

    def open(self, widget):
        self._host.open_in(widget, self._pane.index)

    def label(self, title, widget=None):
        where = self._host.pane_of(widget) if widget is not None else None
        if where is None:
            return title
        return title if where == self._pane.index else f"{title}  (Workspace {where + 1})"


class WorkspaceHost(QMainWindow):
    # Something about the arrangement changed (tabs added/removed/moved/
    # reordered, a different tab selected, workspaces added/closed): the
    # owner debounces these into a save -- see workspace_persistence.py.
    layout_changed = Signal()
    # The window is closing; emitted FIRST, before anything is torn down, so
    # the owner can save the arrangement while it is still intact.
    closing = Signal()

    def __init__(self):
        global _HOST
        super().__init__()
        self.setWindowTitle("TORCA")
        self._entries = {}       # widget -> _Entry
        self._menu_providers = []  # fn(menu, ctx) -> None, see add_menu_provider
        self._placement_hints = {}  # tab key -> workspace index, from a saved arrangement (see restore_state)
        self._floating_menu_items = []
        self._dashboard = None
        self._active_pane = None
        self._closing = False
        self._hide_watcher = _HideWatcher(self)

        # Hidden parent for panes that are not part of the current layout.
        self._holder = QWidget(self)
        self._holder.hide()

        self.panes = [WorkspacePane(i) for i in range(MAX_WORKSPACES)]
        for pane in self.panes:
            pane.tabBar().customContextMenuRequested.connect(
                lambda pos, p=pane: self._on_tab_context_menu(p, pos)
            )
            pane.tabCloseRequested.connect(lambda i, p=pane: self._on_tab_close_requested(p, i))
            pane.set_menu_builder(lambda menu, p=pane: self._fill_add_menu(menu, p))
            pane.set_workspace_menu_builder(lambda menu, p=pane: self._fill_workspace_menu(menu, p))
            pane.currentChanged.connect(lambda _i, p=pane: self._set_active_pane(p))
            pane.currentChanged.connect(lambda _i: self.layout_changed.emit())
            pane.tabBar().tabMoved.connect(lambda _a, _b: self.layout_changed.emit())
        self._active_pane = self.panes[0]

        # ---- Top bar: floating-window menu ----
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        self._floating_button = QToolButton()
        self._floating_button.setText("Floating windows")
        self._floating_button.setPopupMode(QToolButton.InstantPopup)
        self._floating_button.setMenu(QMenu(self._floating_button))
        self._floating_button.menu().aboutToShow.connect(self._rebuild_floating_menu)
        bar.addWidget(self._floating_button)
        bar.addStretch()
        self._top_bar = bar  # widgets handed over by the dashboard are added at its right end

        self._layout_root = QWidget()
        self._layout_box = QVBoxLayout(self._layout_root)
        self._layout_box.setContentsMargins(0, 0, 0, 0)
        self._body = None  # current splitter tree / single pane

        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addLayout(bar)
        outer.addWidget(self._layout_root, stretch=1)
        self.setCentralWidget(central)

        # Start with a single workspace; the operator grows the grid from
        # each workspace's own "Workspace N" menu.
        self._tree = 0  # the split tree of workspaces -- see tree_add / tree_remove
        self._build_layout()

        QApplication.instance().focusChanged.connect(self._on_focus_changed)
        _HOST = self

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    @property
    def workspace_count(self):
        return len(tree_leaves(self._tree))

    def visible_panes(self):
        return [self.panes[i] for i in sorted(tree_leaves(self._tree))]

    def _unused_index(self):
        used = set(tree_leaves(self._tree))
        return min(i for i in range(MAX_WORKSPACES) if i not in used)

    def can_add_workspace(self, index=0, direction="right", scope="this"):
        """True if a workspace can be added that way without the layout
        growing past four workspaces, two wide and two tall."""
        if self.workspace_count >= MAX_WORKSPACES or index not in tree_leaves(self._tree):
            return False
        return fits_grid(tree_add(self._tree, index, direction, scope, self._unused_index()))

    def add_workspace(self, index, direction, scope="this"):
        """Adds an empty workspace to the 'right' of or 'below' workspace
        `index` alone (scope "this") or the whole layout (scope "all"); it
        splits equally with the workspaces it shares a split with. It takes
        the lowest unused workspace number. Returns its index, or None if
        that would exceed four workspaces or two across / two down."""
        if not self.can_add_workspace(index, direction, scope):
            return None
        new_index = self._unused_index()
        self._tree = tree_add(self._tree, index, direction, scope, new_index)
        self._build_layout()
        return new_index

    def _add_options(self, index):
        """The distinct ways workspace `index` can grow a neighbour, as
        [(direction, scope)] -- options that would give the identical layout
        (e.g. right of "this" and right of "all" when this is the last
        column) are listed once, and options that would make the layout wider
        or taller than 2x2 are not offered at all."""
        if self.workspace_count >= MAX_WORKSPACES or index not in tree_leaves(self._tree):
            return []
        new_index = self._unused_index()
        seen, options = set(), []
        for direction in ("right", "below"):
            for scope in ("this", "all"):
                result = tree_add(self._tree, index, direction, scope, new_index)
                if fits_grid(result) and result not in seen:
                    seen.add(result)
                    options.append((direction, scope))
        return options

    def close_workspace(self, index):
        """Removes workspace `index` (never workspace 1). Its tabs are
        MOVED into the remaining workspaces -- the one with the fewest
        tabs each time -- nothing is closed and radios keep running."""
        if index == 0 or index not in tree_leaves(self._tree):
            return False
        self._tree = tree_remove(self._tree, index)
        remaining = self.visible_panes()
        for entry in [e for e in self._entries.values() if e.pane is self.panes[index]]:
            self._move_to_pane(entry, min(remaining, key=lambda p: (p.count(), p.index)))
        self._build_layout()
        if self._active_pane is self.panes[index]:
            self._set_active_pane(self.panes[0])
        return True

    def set_workspace_count(self, n):
        """Programmatic arrangement of `n` workspaces in a standard layout
        (see tree_for_count), moving tabs out of any workspace that goes
        away. The UI itself grows and shrinks the layout one workspace at a
        time (add_workspace / close_workspace)."""
        n = min(max(int(n), 1), MAX_WORKSPACES)
        for index in range(n, MAX_WORKSPACES):
            if index in tree_leaves(self._tree):
                remaining = [self.panes[i] for i in range(n)]
                for entry in [e for e in self._entries.values() if e.pane is self.panes[index]]:
                    self._move_to_pane(entry, min(remaining, key=lambda p: (p.count(), p.index)))
        self._tree = tree_for_count(n)
        self._build_layout()
        if self._active_pane.index not in tree_leaves(self._tree):
            self._set_active_pane(self.panes[0])

    def _fill_workspace_menu(self, menu, pane):
        """The menu behind a workspace's "Workspace N" label."""
        index = pane.index
        options = self._add_options(index)
        count = self.workspace_count
        everyone = "Both" if count == 2 else "All"
        for direction in ("right", "below"):
            for_direction = [scope for d, scope in options if d == direction]
            for scope in for_direction:
                if len(for_direction) == 1:
                    text = f"Add Workspace {direction.capitalize()}"
                elif scope == "this":
                    text = f"Add Workspace {direction.capitalize()} of This" if direction == "right" else "Add Workspace Below This"
                else:
                    text = f"Add Workspace {direction.capitalize()} of {everyone}" if direction == "right" else f"Add Workspace Below {everyone}"
                action = menu.addAction(text)
                action.setToolTip(
                    "Shares space equally with the workspaces it sits among."
                    if scope == "all" or count > 1 else "Splits this workspace in two.")
                action.triggered.connect(lambda _c=False, d=direction, sc=scope: self.add_workspace(index, d, sc))
        if index != 0:
            if not menu.isEmpty():
                menu.addSeparator()
            action = menu.addAction("Close Workspace")
            action.setToolTip("Its tabs move to the other workspaces; nothing is closed.")
            action.triggered.connect(lambda _c=False: self.close_workspace(index))
        elif menu.isEmpty():
            # Nothing fits next to / under THIS one: either all four exist, or the
            # remaining room (the 2x2 limit) is beside another workspace.
            full = self.workspace_count >= MAX_WORKSPACES
            action = menu.addAction(
                f"All {MAX_WORKSPACES} workspaces are in use" if full
                else "Add more from the other workspaces' menus"
            )
            action.setEnabled(False)

    def _build_layout(self):
        """Builds the splitter tree for the current layout; every child of a
        split gets an equal share."""
        self.layout_changed.emit()
        for pane in self.panes:
            pane.setParent(self._holder)  # detach from any old splitter before it is discarded
            pane.hide()
        old_body, self._body = self._body, None
        # Only splitters are throwaway. With one workspace the "body" IS
        # pane 0 itself -- a persistent pane holding the dashboard and its
        # running worker threads -- and must never be deleted (doing so
        # aborted the app: "QThread: Destroyed while thread is still
        # running"). The panes were already detached above, so a discarded
        # splitter tree no longer owns any of them.
        if isinstance(old_body, QSplitter):
            old_body.hide()
            old_body.setParent(None)
            old_body.deleteLater()

        def build(node):
            if isinstance(node, int):
                return self.panes[node]
            orientation, children = node
            return self._splitter(Qt.Horizontal if orientation == H else Qt.Vertical, *[build(c) for c in children])

        body = build(self._tree)
        self._body = body
        self._layout_box.addWidget(body)
        for pane in self.visible_panes():
            pane.show()
        body.show()
        # Equal shares, as real pixel sizes: now (the container already has its
        # size) and once more after the layout pass for a window not yet shown.
        self._equalize_splitters()
        QTimer.singleShot(0, self._equalize_splitters)

    def _equalize_splitters(self):
        """Gives every child of every split an equal share of it. Sizes are
        computed top-down from the layout container's real size (a nested
        splitter has no geometry yet right after it is built), and applied as
        pixels -- QSplitter.setSizes([1, 1]) is NOT treated as a ratio once
        the window is showing."""
        if self._body is None or not isinstance(self._body, QSplitter):
            return
        root = self._layout_root
        self._equalize(self._body, root.width(), root.height())

    def _equalize(self, splitter, width, height):
        children = [splitter.widget(i) for i in range(splitter.count())]
        horizontal = splitter.orientation() == Qt.Horizontal
        total = (width if horizontal else height) - splitter.handleWidth() * (len(children) - 1)
        share = max(total // len(children), 1)
        splitter.setSizes([share] * len(children))
        for child in children:
            if isinstance(child, QSplitter):
                self._equalize(child, share if horizontal else width, height if horizontal else share)

    @staticmethod
    def _splitter(orientation, *children):
        splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)
        for child in children:
            splitter.addWidget(child)
        return splitter

    # ------------------------------------------------------------------
    # Hosting windows
    # ------------------------------------------------------------------

    def register_window(self, widget, closable=True):
        """Makes `widget` known to the host WITHOUT showing it anywhere
        yet -- for tabs the operator opens later from a workspace's "+"
        menu."""
        entry = self._entries.get(widget)
        if entry is None:
            entry = _Entry(widget, closable)
            self._entries[widget] = entry
            widget.installEventFilter(self._hide_watcher)
            widget.windowTitleChanged.connect(lambda _t, w=widget: self._refresh_title(w))
        return entry

    def add_window(self, widget, closable=True, pane_index=None):
        """Registers `widget` and docks it (without stealing focus
        rules from present()). closable=False (the dashboard) removes
        the tab's close button and the pop-out menu item."""
        entry = self.register_window(widget, closable)
        pane = self.panes[pane_index] if pane_index is not None else self._pane_for(widget)
        self._dock(entry, pane)
        return entry

    def open_in(self, widget, pane_index):
        """Opens `widget` as a tab in a SPECIFIC workspace (what picking
        something from that workspace's "+" menu does): docks it there
        if it isn't open, otherwise moves its tab there."""
        entry = self.register_window(widget)
        pane = self.panes[pane_index]
        if entry.floating:
            self.dock_floating(widget)
            entry = self._entries[widget]
        if entry.pane is None:
            self._dock(entry, pane)
        elif entry.pane is not pane:
            self._move_to_pane(entry, pane)
        pane.setCurrentWidget(entry.page)
        self._set_active_pane(pane)
        widget.show()

    def add_menu_provider(self, provider):
        """provider(menu, ctx) is called each time a workspace's "+" menu
        opens and adds its own actions/submenus to `menu`; an action
        opens a widget in that workspace via ctx.open(widget). ctx.label(
        title, widget) marks entries already open elsewhere. Providers
        build widgets lazily (inside the action's handler), so listing
        something here costs nothing until it is picked."""
        self._menu_providers.append(provider)

    def _fill_add_menu(self, menu, pane):
        ctx = _MenuContext(self, pane)
        for provider in self._menu_providers:
            if not menu.isEmpty():
                menu.addSeparator()
            provider(menu, ctx)

    def add_dashboard(self, dashboard, pane_index=0):
        """Docks the dashboard (non-closable) and wires it into the
        workspaces' "+" menus: if it offers take_data_pages(), its
        reference-data sections (Band Conditions, Contests, ...) are
        registered as closable tabs the operator opens when and where
        they want -- none are opened automatically -- and if it offers
        populate_workspace_menu(menu, ctx) that adds its own entries too.
        Closing the host runs the dashboard's shutdown (see
        set_dashboard)."""
        if getattr(dashboard, "workspace_key", None) is None:
            dashboard.workspace_key = "map"
        self.add_window(dashboard, closable=False, pane_index=pane_index)
        self.set_dashboard(dashboard)
        take = getattr(dashboard, "take_data_pages", None)
        if take is not None:
            for title, page in take():
                page.setWindowTitle(title)
                page.workspace_key = f"page:{title}"
                self.register_window(page, closable=True)
        take_top_bar = getattr(dashboard, "take_top_bar_widgets", None)
        if take_top_bar is not None:
            for widget in take_top_bar():
                self._top_bar.addWidget(widget)  # after the stretch: right-aligned, same row as "Floating windows"
        populate = getattr(dashboard, "populate_workspace_menu", None)
        if populate is not None:
            self.add_menu_provider(populate)
        self.present(dashboard)

    def present(self, widget):
        """Shows `widget`: selects its tab if docked, raises it if
        floating, otherwise docks it per the placement rule."""
        entry = self._entries.get(widget)
        if entry is None:
            entry = self.add_window(widget)
        elif entry.floating:
            widget.show()
            widget.raise_()
            widget.activateWindow()
            return
        elif entry.pane is None:
            self._dock(entry, self._pane_for(widget))
        entry.pane.setCurrentWidget(entry.page)
        self._set_active_pane(entry.pane)
        widget.setFocus()

    def _pane_for(self, widget):
        """Where a window being docked for the first time goes: the
        workspace it had in the saved arrangement being restored (see
        restore_state), else the normal placement rule."""
        key = getattr(widget, "workspace_key", None)
        index = self._placement_hints.pop(key, None) if key else None
        if index is not None and index in tree_leaves(self._tree):
            return self.panes[index]
        return self._choose_pane()

    def _choose_pane(self):
        for pane in self.visible_panes():
            if pane.count() == 0:
                return pane
        return self._active_pane if self._active_pane in self.visible_panes() else self.panes[0]

    def _title_for(self, widget):
        return _short_title(widget.windowTitle() or "Window")

    def _dock(self, entry, pane):
        widget = entry.widget
        if entry.floating:
            entry.floating = False
        page = QScrollArea()
        page.setWidgetResizable(True)
        page.setFrameShape(QScrollArea.NoFrame)
        page.setWidget(widget)
        entry.page = page
        self._attach_page(entry, pane)
        widget.show()

    def _attach_page(self, entry, pane):
        index = pane.addTab(entry.page, self._title_for(entry.widget))
        pane.setTabToolTip(index, entry.widget.windowTitle())
        if not entry.closable:
            pane.tabBar().setTabButton(index, QTabBar.RightSide, None)
        entry.pane = pane
        pane.setCurrentIndex(index)
        self.layout_changed.emit()

    def _move_to_pane(self, entry, target):
        """Moves an already-docked window's tab to another workspace."""
        source = entry.pane
        if source is None or source is target:
            return
        source.removeTab(source.indexOf(entry.page))
        self._attach_page(entry, target)  # also makes it the target's current tab

    def move_window(self, widget, pane_index):
        entry = self._entries.get(widget)
        if entry is not None and entry.pane is not None:
            self._move_to_pane(entry, self.panes[pane_index])
            self._set_active_pane(self.panes[pane_index])

    def pane_of(self, widget):
        entry = self._entries.get(widget)
        return entry.pane.index if entry is not None and entry.pane is not None else None

    def _refresh_title(self, widget):
        entry = self._entries.get(widget)
        if entry is None or entry.pane is None:
            return
        index = entry.pane.indexOf(entry.page)
        if index >= 0:
            entry.pane.setTabText(index, self._title_for(widget))
            entry.pane.setTabToolTip(index, widget.windowTitle())

    # ---- closing ----

    def _on_tab_close_requested(self, pane, index):
        page = pane.widget(index)
        for entry in self._entries.values():
            if entry.page is page and entry.closable:
                # The window's own closeEvent decides (stop decoders,
                # disconnect the radio, or hide-as-singleton); the tab is
                # dropped by _widget_hidden once it has actually hidden.
                pane.setCurrentIndex(index)  # so "hidden" can only mean closed, not "not current"
                entry.widget.close()
                return

    def _widget_hidden(self, widget):
        entry = self._entries.get(widget)
        if entry is None or entry.floating or entry.pane is None or self._closing:
            return
        QTimer.singleShot(0, lambda: self._undock(entry))

    def _undock(self, entry):
        """Drops the tab of a window that has hidden itself, leaving the
        widget alive (hidden, parentless) so it can be presented again."""
        if entry.pane is None or not entry.widget.isHidden():
            return  # re-shown in the meantime
        pane, page = entry.pane, entry.page
        pane.removeTab(pane.indexOf(page))
        page.takeWidget()
        entry.widget.setParent(None)
        page.deleteLater()
        entry.pane = entry.page = None
        self.layout_changed.emit()

    # ---- floating ----

    def pop_out(self, widget):
        entry = self._entries.get(widget)
        if entry is None or entry.pane is None or not entry.closable:
            return
        size = widget.size()
        pane, page = entry.pane, entry.page
        pane.removeTab(pane.indexOf(page))
        page.takeWidget()
        page.deleteLater()
        entry.pane = entry.page = None
        entry.floating = True
        widget.setParent(None)
        widget.resize(size)
        widget.show()
        widget.raise_()
        self.layout_changed.emit()

    def dock_floating(self, widget):
        entry = self._entries.get(widget)
        if entry is not None and entry.floating:
            widget.hide()
            self._dock(entry, self._choose_pane())

    def _rebuild_floating_menu(self):
        menu = self._floating_button.menu()
        menu.clear()
        floating = [e for e in self._entries.values() if e.floating and not e.widget.isHidden()]
        if not floating:
            menu.addAction("(none)").setEnabled(False)
            return
        for entry in floating:
            menu.addAction(f"Dock: {entry.widget.windowTitle()}").triggered.connect(
                lambda _c=False, w=entry.widget: self.dock_floating(w)
            )

    # ---- tab context menu ----

    def _on_tab_context_menu(self, pane, pos):
        index = pane.tabBar().tabAt(pos)
        if index < 0:
            return
        entry = next((e for e in self._entries.values() if e.page is pane.widget(index)), None)
        if entry is None:
            return
        menu = QMenu(self)
        for target in self.visible_panes():
            if target is not pane:
                menu.addAction(f"Move to Workspace {target.index + 1}").triggered.connect(
                    lambda _c=False, w=entry.widget, i=target.index: self.move_window(w, i)
                )
        if entry.closable:
            menu.addSeparator()
            menu.addAction("Pop Out to Window").triggered.connect(
                lambda _c=False, w=entry.widget: self.pop_out(w)
            )
            menu.addAction("Close").triggered.connect(
                lambda _c=False, p=pane, w=entry.widget: self._on_tab_close_requested(p, p.indexOf(self._entries[w].page))
            )
        menu.exec(pane.tabBar().mapToGlobal(pos))

    # ---- active workspace ----

    def _set_active_pane(self, pane):
        self._active_pane = pane
        for other in self.panes:
            other.set_active(other is pane)

    def _on_focus_changed(self, _old, new):
        widget = new
        while widget is not None:
            if isinstance(widget, WorkspacePane):
                self._set_active_pane(widget)
                return
            widget = widget.parentWidget()

    # ------------------------------------------------------------------
    # Saved arrangement (see workspace_state.py / workspace_persistence.py)
    # ------------------------------------------------------------------

    def window_keys(self):
        """{key: widget} for every hosted window that has a workspace_key."""
        return {key: entry.widget for entry in self._entries.values()
                if (key := getattr(entry.widget, "workspace_key", None))}

    def capture_state(self):
        """The current arrangement as plain data: the layout tree, and per
        workspace its docked tabs (by key, in order) and selected tab. Only
        windows with a workspace_key are recorded; floating ones are not."""
        workspaces = {}
        for index in tree_leaves(self._tree):
            pane = self.panes[index]
            tabs, current = [], None
            for position in range(pane.count()):
                entry = next((e for e in self._entries.values() if e.page is pane.widget(position)), None)
                key = getattr(entry.widget, "workspace_key", None) if entry else None
                if key:
                    tabs.append(key)
                    if position == pane.currentIndex():
                        current = key
            workspaces[str(index)] = {"tabs": tabs, "current": current}
        return {
            "layout": tree_to_json(self._tree),
            "workspaces": workspaces,
            "active": self._active_pane.index if self._active_pane.index in tree_leaves(self._tree) else 0,
        }

    def restore_state(self, state):
        """Applies a (cleaned) saved arrangement's LAYOUT and records where
        each tab should go. Windows that already exist are placed with
        place_by_hint(); ones created afterwards (radios, tools, the Log Book)
        dock in their saved workspace the first time they are shown. Call
        finish_restore() once everything has been created."""
        tree = tree_from_json(state.get("layout"))
        if tree is None:
            return False
        self._tree = tree
        self._build_layout()
        self._placement_hints = {
            key: int(index) for index, workspace in state.get("workspaces", {}).items() for key in workspace.get("tabs", [])
        }
        return True

    def place_by_hint(self, widget):
        """Puts an already-registered window where the saved arrangement had
        it -- moving it if docked elsewhere, docking it if it was open then.
        Returns True if the saved arrangement had it open."""
        key = getattr(widget, "workspace_key", None)
        index = self._placement_hints.pop(key, None) if key else None
        if index is None or index not in tree_leaves(self._tree):
            return False
        self.open_in(widget, index)
        return True

    def finish_restore(self, state):
        """After everything is created: put each workspace's tabs in their
        saved order, select the saved tab in each, and the saved active
        workspace. Forgets any hints that were never used."""
        self._placement_hints.clear()
        keys = self.window_keys()
        for index_text, workspace in state.get("workspaces", {}).items():
            index = int(index_text)
            if index not in tree_leaves(self._tree):
                continue
            pane = self.panes[index]
            position = 0
            for key in workspace.get("tabs", []):
                widget = keys.get(key)
                entry = self._entries.get(widget)
                if entry is None or entry.pane is not pane:
                    continue
                current_position = pane.indexOf(entry.page)
                if current_position != position:
                    pane.tabBar().moveTab(current_position, position)
                position += 1
            widget = keys.get(workspace.get("current"))
            entry = self._entries.get(widget)
            if entry is not None and entry.pane is pane:
                pane.setCurrentWidget(entry.page)
        active = state.get("active", 0)
        self._set_active_pane(self.panes[active if active in tree_leaves(self._tree) else 0])
        self.layout_changed.emit()

    # ------------------------------------------------------------------
    # Application shutdown
    # ------------------------------------------------------------------

    def set_dashboard(self, dashboard):
        """The window whose closeEvent is the app's full shutdown
        (HamClockWindow): closing the host runs it."""
        self._dashboard = dashboard

    def closeEvent(self, event):
        global _HOST
        self.closing.emit()  # first: lets the owner save the arrangement while it is still intact
        self._closing = True
        if self._dashboard is not None:
            self._dashboard.close()  # radios, servers, worker threads -- see HamClockWindow.closeEvent
        for entry in list(self._entries.values()):
            if entry.floating:
                entry.widget.close()
                entry.widget.hide()
        if _HOST is self:
            _HOST = None
        event.accept()
