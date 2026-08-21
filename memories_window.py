"""
MemoriesWindow: TORCA's own memory-channel system, opened by the
"Memories" button (main_window.py, stacked directly under Split) --
built from scratch because rigplane can't actually read memory channel
data back off the radio (get_memory_mode/get_memory_contents both
unconditionally raise NotImplementedError in this rigplane version,
confirmed by reading runtime/radio.py directly; only the write side --
selecting/writing/clearing a channel blind -- works). So instead of
"channels on the radio", these are named app-side snapshots the
operator captures on demand.

Singleton per RadioWindow, same show-or-raise / hide-not-destroy
pattern as CwToolWindow/SstvToolWindow (cw_window.py/sstv_window.py) --
closeEvent hides unless closing_for_real() was called (the owning
RadioWindow is really going away).

Data model: tabs (named groups) each holding a list of named memory
entries; each entry stores VFO A's and VFO B's frequency/mode,
captured together in one shot via RadioWorker.capture_memory_snapshot
(main_window.py -> radio_worker.py) -- "Add Memory" writes the RADIO's
current live settings into a new entry, the reverse of what the name
might suggest at first glance. Persisted to a shared memories.json
(same ~/.icom_radio_app_cache directory connection_dialog.py/
cw_window.py already use for structured, non-QSettings data) so tabs/
entries carry over across radios and app restarts -- one shared bank,
not per-radio-model, since an operator's named memory list (e.g. "Local
Repeaters", "Satellite Uplinks") is just as useful from whichever radio
window happens to be open.

Recall-to-radio (loading a saved entry back into VFO A/B) is
deliberately NOT included here -- out of scope for what was asked.
"""

import json
import pathlib

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QWidget,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
)

MEMORIES_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "memories.json"

_COLUMNS = ["Name", "VFO A Freq (MHz)", "VFO A Mode", "VFO B Freq (MHz)", "VFO B Mode"]


def _load_memories():
    if MEMORIES_PATH.exists():
        try:
            data = json.loads(MEMORIES_PATH.read_text())
            if isinstance(data, dict) and isinstance(data.get("tabs"), list):
                return data
        except (OSError, ValueError):
            pass
    return {"tabs": [{"name": "Memories", "entries": []}]}


def _save_memories(data):
    MEMORIES_PATH.parent.mkdir(parents=True, exist_ok=True)
    MEMORIES_PATH.write_text(json.dumps(data, indent=2))


def _format_mhz(freq_hz):
    if freq_hz is None:
        return ""
    return f"{freq_hz / 1e6:.6f}"


def _parse_mhz(text):
    try:
        return round(float(text) * 1e6)
    except ValueError:
        return None


class MemoryTabPage(QWidget):
    """One tab's contents: a table of memory entries plus Add/Delete
    Memory buttons. `entries` is the actual list object living inside
    MemoriesWindow._data -- mutated in place so the owning window's
    save() always sees the current state, no separate sync step."""

    # Emitted with the chosen name once the operator confirms Add
    # Memory's name prompt -- MemoriesWindow owns the actual radio
    # capture (it's the one connected to RadioWorker's signal), so this
    # is just "please capture, and call finish_add(name, ...) with the
    # result when it's ready".
    add_requested = Signal(str)

    def __init__(self, entries, on_change):
        super().__init__()
        self._entries = entries
        self._on_change = on_change
        self._populating = False  # guards against itemChanged firing while rows are built programmatically

        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.itemChanged.connect(self._on_item_changed)
        self._reload_rows()

        add_button = QPushButton("Add Memory")
        add_button.setToolTip("Captures the radio's current VFO A/B frequency and mode into a new, named entry.")
        add_button.clicked.connect(self._on_add_clicked)
        delete_button = QPushButton("Delete Memory")
        delete_button.clicked.connect(self._on_delete_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(add_button)
        button_row.addWidget(delete_button)
        button_row.addStretch()

        layout = QVBoxLayout()
        layout.addWidget(self.table)
        layout.addLayout(button_row)
        self.setLayout(layout)

    def _reload_rows(self):
        self._populating = True
        self.table.setRowCount(0)
        for entry in self._entries:
            self._append_row(entry)
        self._populating = False

    def _append_row(self, entry):
        row = self.table.rowCount()
        self.table.insertRow(row)
        vfo_a = entry.get("vfo_a") or {}
        vfo_b = entry.get("vfo_b") or {}
        values = [
            entry.get("name", ""),
            _format_mhz(vfo_a.get("freq_hz")),
            vfo_a.get("mode") or "",
            _format_mhz(vfo_b.get("freq_hz")),
            vfo_b.get("mode") or "",
        ]
        for col, value in enumerate(values):
            self.table.setItem(row, col, QTableWidgetItem(value))

    def _on_item_changed(self, item):
        if self._populating:
            return
        row = item.row()
        if row >= len(self._entries):
            return
        entry = self._entries[row]
        text = item.text().strip()
        col = item.column()
        if col == 0:
            entry["name"] = text
        elif col == 1:
            entry.setdefault("vfo_a", {})["freq_hz"] = _parse_mhz(text)
        elif col == 2:
            entry.setdefault("vfo_a", {})["mode"] = text
        elif col == 3:
            entry.setdefault("vfo_b", {})["freq_hz"] = _parse_mhz(text)
        elif col == 4:
            entry.setdefault("vfo_b", {})["mode"] = text
        self._on_change()

    def _on_add_clicked(self):
        name, ok = QInputDialog.getText(self, "Add Memory", "Name:")
        if not ok:
            return
        self.add_requested.emit(name.strip() or f"Memory {len(self._entries) + 1}")

    def finish_add(self, name, snapshot):
        """Called by MemoriesWindow once the radio capture triggered by
        add_requested actually comes back."""
        entry = {"name": name, "vfo_a": dict(snapshot["A"]), "vfo_b": dict(snapshot["B"])}
        self._entries.append(entry)
        self._populating = True
        self._append_row(entry)
        self._populating = False
        self._on_change()

    def _on_delete_clicked(self):
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            QMessageBox.information(self, "Delete Memory", "Select one or more memories in the list first.")
            return
        names = ", ".join(self._entries[row].get("name") or "(unnamed)" for row in rows)
        confirm = QMessageBox.question(
            self, "Delete Memory", f"Delete {len(rows)} memory(ies)?\n{names}",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        for row in rows:
            del self._entries[row]
            self.table.removeRow(row)
        self._on_change()


class MemoriesWindow(QWidget):
    def __init__(self, radio_window):
        super().__init__()
        self._radio_window = radio_window
        self.setWindowTitle(f"Memories -- {radio_window._connection_label}")
        self.resize(640, 420)
        self._closing_for_real = False
        self._data = _load_memories()
        # Set right before triggering a capture (_on_add_requested),
        # read back once memory_snapshot_captured actually arrives --
        # capture is async (a real round trip to the radio), so this is
        # how finish_add() knows which tab/name it's for.
        self._capture_target_page = None
        self._capture_target_name = None

        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.tabCloseRequested.connect(self._on_tab_close_requested)
        self.tabs.tabBarDoubleClicked.connect(self._on_tab_double_clicked)

        add_tab_button = QPushButton("+")
        add_tab_button.setFixedWidth(28)
        add_tab_button.setToolTip("Add a new tab")
        add_tab_button.clicked.connect(self._on_add_tab_clicked)
        self.tabs.setCornerWidget(add_tab_button)

        for tab in self._data["tabs"]:
            self._add_tab_page(tab["name"], tab["entries"])

        layout = QVBoxLayout()
        layout.addWidget(self.tabs)
        self.setLayout(layout)

        radio_window.worker.memory_snapshot_captured.connect(self._on_memory_snapshot_captured)

    def _add_tab_page(self, name, entries):
        page = MemoryTabPage(entries, self._save)
        page.add_requested.connect(lambda memory_name, p=page: self._on_add_requested(p, memory_name))
        self.tabs.addTab(page, name)
        return page

    def _save(self):
        _save_memories(self._data)

    def _on_add_tab_clicked(self):
        name, ok = QInputDialog.getText(self, "Add Tab", "Tab name:")
        if not ok:
            return
        name = name.strip() or "New Tab"
        entries = []
        self._data["tabs"].append({"name": name, "entries": entries})
        page = self._add_tab_page(name, entries)
        self.tabs.setCurrentWidget(page)
        self._save()

    def _on_tab_double_clicked(self, index):
        if index < 0:
            return  # double-click landed past the last tab (e.g. on the corner "+" button), not on a real tab
        current_name = self.tabs.tabText(index)
        name, ok = QInputDialog.getText(self, "Rename Tab", "Tab name:", text=current_name)
        if not ok:
            return
        name = name.strip() or current_name
        self.tabs.setTabText(index, name)
        self._data["tabs"][index]["name"] = name
        self._save()

    def _on_tab_close_requested(self, index):
        name = self.tabs.tabText(index)
        confirm = QMessageBox.question(
            self, "Delete Tab", f'Delete tab "{name}" and all its memories?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        self.tabs.removeTab(index)
        del self._data["tabs"][index]
        self._save()

    def _on_add_requested(self, page, name):
        self._capture_target_page = page
        self._capture_target_name = name
        self._radio_window.worker.capture_memory_snapshot()

    def _on_memory_snapshot_captured(self, snapshot):
        page = self._capture_target_page
        name = self._capture_target_name
        self._capture_target_page = None
        self._capture_target_name = None
        if page is None:
            return  # a capture finished after this window already dropped its own request (shouldn't happen -- one worker per window)
        if snapshot is None:
            QMessageBox.warning(
                self, "Add Memory",
                "Couldn't capture the radio's current VFO A/B settings -- see the status/error log.",
            )
            return
        page.finish_add(name, snapshot)

    # ---- Lifecycle ----

    def closing_for_real(self):
        """Called by RadioWindow.closeEvent when the OWNING radio
        window is actually going away for good -- lets closeEvent below
        really close instead of its usual hide-and-keep-the-singleton-
        alive behavior."""
        self._closing_for_real = True
        self.close()

    def closeEvent(self, event):
        if self._closing_for_real:
            event.accept()
        else:
            event.ignore()
            self.hide()
