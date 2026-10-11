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

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer, Signal
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
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

PLUS_BUTTON_STYLE = (
    "QToolButton { font-weight: bold; font-size: 16px; padding: 0 8px; }"
    "QToolButton::menu-indicator { image: none; }"
)
DEFAULT_WORKSPACES = 2

_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"
_SETTINGS_KEY = "workspace_count"

_TAB_TITLE_MAX = 28

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

        self._badge = QLabel()
        self._badge.setContentsMargins(8, 0, 8, 0)
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
            "color: #e8a33d; font-weight: bold;" if active else "color: #888;"
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
    def __init__(self):
        global _HOST
        super().__init__()
        self.setWindowTitle("TORCA")
        self._entries = {}       # widget -> _Entry
        self._menu_providers = []  # fn(menu, ctx) -> None, see add_menu_provider
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
            pane.currentChanged.connect(lambda _i, p=pane: self._set_active_pane(p))
        self._active_pane = self.panes[0]

        # ---- Top bar: workspace-count selector + floating-window menu ----
        bar = QHBoxLayout()
        bar.setContentsMargins(6, 4, 6, 2)
        bar.addWidget(QLabel("Workspaces:"))
        self._count_group = QButtonGroup(self)
        self._count_buttons = {}
        for n in range(1, MAX_WORKSPACES + 1):
            button = QPushButton(str(n))
            button.setCheckable(True)
            button.setFixedWidth(34)
            button.setStyleSheet("QPushButton:checked { background-color: #c33; color: white; font-weight: bold; }")
            button.setToolTip(f"Show {n} workspace{'s' if n > 1 else ''}")
            self._count_group.addButton(button, n)
            self._count_buttons[n] = button
            bar.addWidget(button)
        self._count_group.idClicked.connect(self.set_workspace_count)
        bar.addSpacing(12)
        self._floating_button = QToolButton()
        self._floating_button.setText("Floating windows")
        self._floating_button.setPopupMode(QToolButton.InstantPopup)
        self._floating_button.setMenu(QMenu(self._floating_button))
        self._floating_button.menu().aboutToShow.connect(self._rebuild_floating_menu)
        bar.addWidget(self._floating_button)
        bar.addStretch()

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

        self._count = 0
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        try:
            saved = int(settings.value(_SETTINGS_KEY, DEFAULT_WORKSPACES))
        except (TypeError, ValueError):
            saved = DEFAULT_WORKSPACES
        self.set_workspace_count(min(max(saved, 1), MAX_WORKSPACES), persist=False)

        QApplication.instance().focusChanged.connect(self._on_focus_changed)
        _HOST = self

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    @property
    def workspace_count(self):
        return self._count

    def visible_panes(self):
        return self.panes[: self._count]

    def set_workspace_count(self, n, persist=True):
        """Shows `n` (1-4) workspaces. Tabs in workspaces that go away
        are MOVED into the remaining ones (the one with the fewest tabs
        each time) -- nothing is closed, radios keep running."""
        n = min(max(int(n), 1), MAX_WORKSPACES)
        for pane in self.panes[n:]:
            for entry in [e for e in self._entries.values() if e.pane is pane]:
                target = min(self.panes[:n], key=lambda p: (p.count(), p.index))
                self._move_to_pane(entry, target)
        self._count = n
        self._count_buttons[n].setChecked(True)
        self._build_layout(n)
        if self._active_pane not in self.panes[:n]:
            self._set_active_pane(self.panes[0])
        if persist:
            QSettings(_SETTINGS_ORG, _SETTINGS_APP).setValue(_SETTINGS_KEY, n)

    def _build_layout(self, n):
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
        p = self.panes
        if n == 1:
            body = p[0]
        elif n == 2:
            body = self._splitter(Qt.Horizontal, p[0], p[1])
        elif n == 3:
            # One large workspace on the left, two stacked on the right.
            body = self._splitter(Qt.Horizontal, p[0], self._splitter(Qt.Vertical, p[1], p[2]))
        else:
            body = self._splitter(
                Qt.Vertical,
                self._splitter(Qt.Horizontal, p[0], p[1]),
                self._splitter(Qt.Horizontal, p[2], p[3]),
            )
        self._body = body
        self._layout_box.addWidget(body)
        for pane in p[:n]:
            pane.show()
        body.show()

    @staticmethod
    def _splitter(orientation, first, second):
        splitter = QSplitter(orientation)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(first)
        splitter.addWidget(second)
        splitter.setSizes([1, 1])
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
        pane = self.panes[pane_index] if pane_index is not None else self._choose_pane()
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
        self.add_window(dashboard, closable=False, pane_index=pane_index)
        self.set_dashboard(dashboard)
        take = getattr(dashboard, "take_data_pages", None)
        if take is not None:
            for title, page in take():
                page.setWindowTitle(title)
                self.register_window(page, closable=True)
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
            self._dock(entry, self._choose_pane())
        entry.pane.setCurrentWidget(entry.page)
        self._set_active_pane(entry.pane)
        widget.setFocus()

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
    # Application shutdown
    # ------------------------------------------------------------------

    def set_dashboard(self, dashboard):
        """The window whose closeEvent is the app's full shutdown
        (HamClockWindow): closing the host runs it."""
        self._dashboard = dashboard

    def closeEvent(self, event):
        global _HOST
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
