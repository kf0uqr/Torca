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

Local Repeaters tab: a special auto-populated tab (kind="local_
repeaters" in the persisted data, vs. plain "manual" tabs) that imports
nearby 2m/70cm repeaters from a RepeaterBook CSV export the operator
downloads themselves (repeater_import.py -- no API call, no key, see
its own docstring for why), filtered by distance from the operator's
saved GPS location (same operator_lat/operator_lon QSettings
OperatorProfileDialog/ham_dashboard.py already use), and turns each
into a memory entry -- VFO A (RX) is the repeater's output/downlink,
VFO B (TX) is input/uplink (output + offset). This app previously
queried a couple of live repeater-directory APIs directly (first
RepeaterBook, then Open Repeater after RepeaterBook's terms turned out
to disallow exactly this use case) -- both abandoned: RepeaterBook's
API requires an approved app token and its own docs explicitly say
"nearby repeater discovery tools" aren't authorized without written
permission, and Open Repeater's free-tier signup didn't work out for a
real operator using this app. A manually-downloaded CSV sidesteps both
problems at once. Otherwise behaves exactly like any other tab
(editable, deletable, supports manual Add/Delete Memory too) -- only
Refresh (re-import, replacing every entry) is unique to it.
"""

import json
import pathlib

from PySide6.QtCore import Qt, QSettings, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
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
    QFileDialog,
)

import repeater_import

MEMORIES_PATH = pathlib.Path.home() / ".icom_radio_app_cache" / "memories.json"

# Same QSettings org/app every other structured-non-JSON setting in
# this codebase uses (connection_dialog.py, log_book_window.py, ...).
_SETTINGS_ORG = "IcomRadioApp"
_SETTINGS_APP = "RadioControl"

_COLUMNS = [
    "Name", "VFO A Freq (MHz)", "VFO A Mode", "VFO B Freq (MHz)", "VFO B Mode",
    "Tone Mode", "Tone Freq (Hz)",
]
# Tone Mode values, same shape split_dialog.py's Repeater Tone section
# already uses: "none" (no tone), "tone" (transmit tone only), "tsql"
# (tone squelch -- transmits AND uses the same frequency as a receive
# squelch, per wfview's own description, already the convention this
# app follows for split_dialog.py's Repeater Tone section).

# Default/typical "local repeater" search radius -- roughly 50 miles,
# a common rule-of-thumb VHF/UHF repeater search distance. Just a
# starting value in the radius prompt, not a hard limit -- the operator
# can type any value 1-500 there.
_DEFAULT_REPEATER_RADIUS_KM = 80


def _repeater_to_entry(repeater):
    name = repeater["callsign"] or "Repeater"
    if repeater.get("city"):
        name = f"{name} ({repeater['city']})"
    ctcss_hz = repeater.get("ctcss_hz")
    # A repeater with a published CTCSS tone almost universally needs
    # it on transmit to access it -- "tsql" (per wfview's own
    # description: "a tone is transmitted, and the same tone frequency
    # is used as a tone squelch") is the same convention split_dialog.py's
    # Repeater Tone section already uses for "has a tone" in general.
    tone = {"mode": "tsql", "freq_hz": ctcss_hz} if ctcss_hz else {"mode": "none", "freq_hz": None}
    return {
        "name": name,
        "vfo_a": {"freq_hz": repeater["output_freq_hz"], "mode": repeater["mode"], "filter": None},
        "vfo_b": {"freq_hz": repeater["input_freq_hz"], "mode": repeater["mode"], "filter": None},
        "tone": tone,
    }


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


def all_memory_markers():
    """Every saved memory entry's own frequency, flattened across every
    tab, for widgets.py's BandPlanOverlayWidget to draw as tick marks
    on the band-plan strip. Reads MEMORIES_PATH directly (same as
    _load_memories) rather than requiring a live MemoriesWindow
    instance -- the overlay needs this on every RadioWindow regardless
    of whether the operator has ever opened the Memories window in
    this session at all.

    Only VFO A (the RX frequency) is surfaced -- VFO B only differs for
    repeater-style split entries, and "where does this memory sit in
    the band" is naturally a receive-frequency question. Returns
    [{"name", "freq_hz"}, ...], skipping any entry with no VFO A
    frequency recorded (a freshly added-but-uncaptured entry, or a bad
    hand edit of memories.json)."""
    data = _load_memories()
    markers = []
    for tab in data.get("tabs", []):
        for entry in tab.get("entries", []):
            freq_hz = (entry.get("vfo_a") or {}).get("freq_hz")
            if freq_hz:
                markers.append({"name": entry.get("name") or "(unnamed)", "freq_hz": freq_hz})
    return markers


def _format_mhz(freq_hz):
    if freq_hz is None:
        return ""
    return f"{freq_hz / 1e6:.6f}"


def _parse_mhz(text):
    try:
        return round(float(text) * 1e6)
    except ValueError:
        return None


def _format_tone_hz(freq_hz):
    if freq_hz is None:
        return ""
    return f"{freq_hz:.1f}"


def _parse_tone_hz(text):
    try:
        return float(text)
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
        add_button.setToolTip("Captures the radio's current VFO A/B frequency/mode and repeater tone into a new, named entry.")
        add_button.clicked.connect(self._on_add_clicked)
        delete_button = QPushButton("Delete Memory")
        delete_button.clicked.connect(self._on_delete_clicked)

        button_row = QHBoxLayout()
        button_row.addWidget(add_button)
        button_row.addWidget(delete_button)
        for extra_button in self._extra_buttons():
            button_row.addWidget(extra_button)
        button_row.addStretch()

        layout = QVBoxLayout()
        layout.addWidget(self.table)
        layout.addLayout(button_row)
        self.setLayout(layout)

    def _extra_buttons(self):
        """Hook for subclasses (LocalRepeatersTabPage) to add buttons
        of their own alongside Add/Delete Memory -- called during
        __init__, before self.table exists, so subclasses that need it
        must set up their own state first (see LocalRepeatersTabPage)."""
        return []

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
        tone = entry.get("tone") or {}
        values = [
            entry.get("name", ""),
            _format_mhz(vfo_a.get("freq_hz")),
            vfo_a.get("mode") or "",
            _format_mhz(vfo_b.get("freq_hz")),
            vfo_b.get("mode") or "",
            tone.get("mode") or "none",
            _format_tone_hz(tone.get("freq_hz")),
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
        elif col == 5:
            entry.setdefault("tone", {})["mode"] = text.lower() or "none"
        elif col == 6:
            entry.setdefault("tone", {})["freq_hz"] = _parse_tone_hz(text)
        self._on_change()

    def _on_add_clicked(self):
        name, ok = QInputDialog.getText(self, "Add Memory", "Name:")
        if not ok:
            return
        self.add_requested.emit(name.strip() or f"Memory {len(self._entries) + 1}")

    def finish_add(self, name, snapshot):
        """Called by MemoriesWindow once the radio capture triggered by
        add_requested actually comes back."""
        entry = {
            "name": name,
            "vfo_a": dict(snapshot["A"]),
            "vfo_b": dict(snapshot["B"]),
            "tone": dict(snapshot.get("tone") or {"mode": "none", "freq_hz": None}),
        }
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

    def replace_entries(self, new_entries):
        """Wholesale replace -- used by LocalRepeatersTabPage's Refresh
        (and reusable by anything else that wants a "re-populate this
        tab from scratch" operation later)."""
        self._entries[:] = new_entries
        self._reload_rows()
        self._on_change()


class LocalRepeatersTabPage(MemoryTabPage):
    """Same table/Add/Delete Memory shape as MemoryTabPage -- entries
    are just as user-editable/deletable here as in any other tab --
    plus a Refresh button that re-imports nearby 2m/70cm repeaters from
    a RepeaterBook CSV export (see MemoriesWindow._fetch_local_repeater_
    entries) and replaces this tab's entries wholesale."""

    def __init__(self, entries, on_change, on_refresh):
        # Set before super().__init__() -- _extra_buttons() (called
        # from within it) needs this already available.
        self._on_refresh = on_refresh
        super().__init__(entries, on_change)

    def _extra_buttons(self):
        refresh_button = QPushButton("Refresh")
        refresh_button.setToolTip(
            "Re-imports nearby 2m/70cm repeaters from a RepeaterBook CSV export you pick, "
            "replacing every entry currently in this tab -- any manual edits/additions here "
            "are discarded."
        )
        refresh_button.clicked.connect(self._on_refresh_clicked)
        return [refresh_button]

    def _on_refresh_clicked(self):
        if self._entries:
            confirm = QMessageBox.question(
                self, "Refresh Local Repeaters",
                "This replaces every entry currently in this tab with a fresh import. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if confirm != QMessageBox.Yes:
                return
        self._on_refresh(self)


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
        add_tab_button.setToolTip("Add a new (blank) tab")
        add_tab_button.clicked.connect(self._on_add_tab_clicked)

        add_repeaters_tab_button = QPushButton("+ Local Repeaters")
        add_repeaters_tab_button.setToolTip(
            "Add a tab auto-populated with nearby 2m/70cm repeaters, imported from a "
            "RepeaterBook CSV export you already have (download one from RepeaterBook.com's "
            "own search results page, then pick it here -- no API key or account needed)."
        )
        add_repeaters_tab_button.clicked.connect(self._on_add_local_repeaters_tab_clicked)

        corner_widget = QWidget()
        corner_layout = QHBoxLayout(corner_widget)
        corner_layout.setContentsMargins(0, 0, 0, 0)
        corner_layout.addWidget(add_tab_button)
        corner_layout.addWidget(add_repeaters_tab_button)
        self.tabs.setCornerWidget(corner_widget)

        for tab in self._data["tabs"]:
            self._add_tab_page(tab["name"], tab["entries"], tab.get("kind", "manual"))

        layout = QVBoxLayout()
        layout.addWidget(self.tabs)
        self.setLayout(layout)

        radio_window.worker.memory_snapshot_captured.connect(self._on_memory_snapshot_captured)

    def _add_tab_page(self, name, entries, kind="manual"):
        if kind == "local_repeaters":
            page = LocalRepeatersTabPage(entries, self._save, self._on_refresh_local_repeaters)
        else:
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

    # ---- Local Repeaters (RepeaterBook CSV import) ----

    def _fetch_local_repeater_entries(self):
        """Prompts for a search radius and a CSV file, imports it, and
        returns a list of memory-entry dicts (see _repeater_to_entry)
        -- or None if the operator cancelled, or a real failure was
        already reported via QMessageBox. Shared by both "+ Local
        Repeaters" (new tab) and Refresh (existing tab).

        No API/network access at all -- the operator downloads a CSV
        export themselves from RepeaterBook.com's own search results
        page (their own personal, manual, one-time use of the site,
        same as browsing it in any other tab) and this just reads
        whatever's already on disk, then filters it to "near me"
        locally. This app deliberately never calls RepeaterBook's own
        API directly -- their current published terms explicitly
        disallow exactly this kind of automated "nearby repeater
        discovery tool" without separate written approval (see
        repeater_import.py's own docstring for the full citation)."""
        settings = QSettings(_SETTINGS_ORG, _SETTINGS_APP)
        lat = float(settings.value("operator_lat", 0.0)) or None
        lon = float(settings.value("operator_lon", 0.0)) or None
        if lat is None or lon is None:
            QMessageBox.warning(
                self, "Local Repeaters",
                "No GPS location saved yet -- set it in Profile... first "
                "(Get GPS Coordinates, or enter it manually).",
            )
            return None

        radius_km, ok = QInputDialog.getInt(
            self, "Local Repeaters", "Search radius (km):", _DEFAULT_REPEATER_RADIUS_KM, 1, 500
        )
        if not ok:
            return None

        path, _ = QFileDialog.getOpenFileName(
            self, "Import RepeaterBook CSV", "", "CSV files (*.csv);;All files (*)"
        )
        if not path:
            return None
        try:
            repeaters = repeater_import.parse_repeater_csv(path)
        except Exception as exc:
            QMessageBox.warning(self, "Local Repeaters", f"Couldn't read that file: {exc}")
            return None
        repeaters = repeater_import.filter_by_distance(repeaters, lat, lon, radius_km)

        if not repeaters:
            QMessageBox.information(self, "Local Repeaters", "No 2m/70cm repeaters found within range.")
        return [_repeater_to_entry(repeater) for repeater in repeaters]

    def _on_add_local_repeaters_tab_clicked(self):
        entries = self._fetch_local_repeater_entries()
        if entries is None:
            return
        name = "Local Repeaters"
        self._data["tabs"].append({"name": name, "kind": "local_repeaters", "entries": entries})
        page = self._add_tab_page(name, entries, kind="local_repeaters")
        self.tabs.setCurrentWidget(page)
        self._save()

    def _on_refresh_local_repeaters(self, page):
        entries = self._fetch_local_repeater_entries()
        if entries is None:
            return
        page.replace_entries(entries)

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
