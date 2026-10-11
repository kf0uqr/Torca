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

from PySide6.QtCore import QEvent, QObject, QSettings, Qt, QTimer
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


class WorkspacePane(QTabWidget):
    def __init__(self, index, parent=None):
        super().__init__(parent)
        self.index = index
        self.setDocumentMode(True)
        self.setMovable(True)
        self.setTabsClosable(True)
        self.setElideMode(Qt.ElideRight)
        self.setUsesScrollButtons(True)
        self.tabBar().setContextMenuPolicy(Qt.CustomContextMenu)

        self._badge = QLabel()
        self._badge.setContentsMargins(8, 0, 8, 0)
        self.setCornerWidget(self._badge, Qt.TopRightCorner)
        self.set_active(False)

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
                self.rect(), Qt.AlignCenter,
                f"Workspace {self.index + 1} — empty\n\nNew radios open here.\n"
                "Right-click a tab in another workspace to move it here.",
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


class WorkspaceHost(QMainWindow):
    def __init__(self):
        global _HOST
        super().__init__()
        self.setWindowTitle("TORCA")
        self._entries = {}       # widget -> _Entry
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

    def add_window(self, widget, closable=True, pane_index=None):
        """Registers `widget` and docks it (without stealing focus
        rules from present()). closable=False (the dashboard) removes
        the tab's close button and the pop-out menu item."""
        entry = self._entries.get(widget)
        if entry is None:
            entry = _Entry(widget, closable)
            self._entries[widget] = entry
            widget.installEventFilter(self._hide_watcher)
            widget.windowTitleChanged.connect(lambda _t, w=widget: self._refresh_title(w))
        pane = self.panes[pane_index] if pane_index is not None else self._choose_pane()
        self._dock(entry, pane)
        return entry

    def add_dashboard(self, dashboard, pane_index=0):
        """Docks the dashboard (non-closable) and, if it offers
        take_data_pages(), its reference-data sections as their own
        non-closable tabs in the SAME workspace, then leaves the
        dashboard itself selected. Closing the host runs the dashboard's
        shutdown (see set_dashboard)."""
        self.add_window(dashboard, closable=False, pane_index=pane_index)
        self.set_dashboard(dashboard)
        take = getattr(dashboard, "take_data_pages", None)
        if take is not None:
            for title, page in take():
                page.setWindowTitle(title)
                self.add_window(page, closable=False, pane_index=pane_index)
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
