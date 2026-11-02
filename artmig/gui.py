"""PySide6 migration console for the Artifactory -> GitHub toolkit.

A desktop cockpit over the same code the CLI drives: it reads each type's
inventory + the resume ledger (manifests/state.json) to show, per package /
image / folder, how much has migrated — and runs the real pipeline for a single
item (or a whole category) in a background thread, streaming the live log.

Nothing here reimplements migration logic. It builds a `Context` exactly like
`migrate.py` does, sets `ctx.item_filter` to scope a run, and calls the type
modules' phase functions (`download`/`transform`/`publish`). Status is read-only
from state.json; only "Refresh inventory" touches Artifactory.

Run it with ``python -m artmig.gui`` (needs ``pip install PySide6``).
"""
from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

from . import data, docker, npm, pypi
from .artifactory import Artifactory
from .config import load_config
from .context import Context
from .github import GitHub
from .state import State
from .util import get_logger

try:
    from PySide6.QtCore import QSize, Qt, QThread, Signal
    from PySide6.QtGui import (
        QAction,
        QBrush,
        QColor,
        QFont,
        QIcon,
        QPainter,
        QPen,
        QPixmap,
    )
    from PySide6.QtWidgets import (
        QApplication,
        QCheckBox,
        QFrame,
        QHBoxLayout,
        QHeaderView,
        QLabel,
        QListWidget,
        QListWidgetItem,
        QMainWindow,
        QMessageBox,
        QPlainTextEdit,
        QProgressBar,
        QPushButton,
        QSizePolicy,
        QSplitter,
        QToolBar,
        QTreeWidget,
        QTreeWidgetItem,
        QVBoxLayout,
        QWidget,
    )
except ImportError as exc:  # pragma: no cover - import-time guard
    raise SystemExit(
        "The migration console needs PySide6.\n"
        "Install it with:  pip install PySide6\n"
        f"(original error: {exc})"
    ) from exc


log = get_logger()

MODULES = {"pypi": pypi, "docker": docker, "npm": npm, "data": data}
# The phases a "Migrate" click runs, mirroring the CLI's `publish` expansion.
PUBLISH_PHASES = ["download", "transform", "publish"]

# ---- palette (mirrors the approved design mockup) ----------------------------
GROUND = "#0F1C1F"
PANEL = "#15282B"
PANEL2 = "#122326"
LINE = "#27413F"
TEXT = "#E8E6DD"
MUTED = "#8AA0A0"
FAINT = "#5E7676"
ACCENT = "#E0A33E"      # signal amber — the action color
DONE = "#4FD1A5"        # verified teal — reserved for "migrated"
FAIL = "#E06A5A"
PEND = "#6E8585"
MONO = 'ui-monospace, "Cascadia Code", "Consolas", "Menlo", monospace'

# Row + action-button sizing. Rows must be tall enough that the button (with its
# padding + border) isn't clipped top/bottom — the button text was truncating.
_BTN_HEIGHT = 28
_ROW_HEIGHT = 38


# =============================================================================
# Status model (pure, no Qt) — rolls inventory + state.json into a view model.
# =============================================================================
@dataclass
class Leaf:
    key: str            # state.json key (e.g. "docker:<ref>")
    label: str          # what the user sees (filename / version / tag / path)
    detail: str = ""    # secondary line


@dataclass
class Item:
    """A top-level migratable unit: a package, a docker image, or a data folder.

    ``ident`` is what we put in ``ctx.item_filter`` to scope a run to just this.
    """
    ident: str
    label: str
    sublabel: str
    leaves: list[Leaf] = field(default_factory=list)


def build_items(ctx: Context, type_name: str, inv: dict) -> list[Item]:
    """Turn a type's inventory JSON into the UI's item/leaf tree.

    Leaf keys are constructed to match exactly what each type module writes to
    state.json, so completion rolls up correctly.
    """
    items: list[Item] = []
    if type_name == "pypi":
        for norm, pkg in sorted(inv.get("packages", {}).items()):
            leaves = [Leaf(f"pypi:{norm}:{f['filename']}", f["filename"],
                           (f.get("sha256") or "")[:16])
                      for f in pkg.get("files", [])]
            items.append(Item(norm, pkg.get("display_name", norm),
                              f"{len(leaves)} files", leaves))
    elif type_name == "npm":
        for name, meta in sorted(inv.get("packages", {}).items()):
            leaves = [Leaf(f"npm:{name}:{v['version']}", v["version"],
                           f"-> {meta.get('new_name', '')}")
                      for v in meta.get("versions", [])]
            items.append(Item(name, name, f"{len(leaves)} versions", leaves))
    elif type_name == "docker":
        registry = ctx.cfg.artifactory.docker_registry
        for key, meta in sorted(inv.get("images", {}).items()):
            repo, image = meta["source_repo"], meta["image"]
            leaves = [Leaf(f"docker:{repo}.{registry}/{image}:{tag}", tag,
                           f"-> ghcr.io/{ctx.cfg.namespace}/{repo}/{image}:{tag}".lower())
                      for tag in meta.get("tags", [])]
            items.append(Item(key, key, f"{len(leaves)} tags", leaves))
    elif type_name == "data":
        folders: dict[str, list[dict]] = {}
        for f in inv.get("files", []):
            folders.setdefault(f["folder"], []).append(f)
        for folder, files in sorted(folders.items()):
            leaves = [Leaf(f"data:{f['path']}", f["path"].split("/", 1)[-1],
                           f"{f.get('size', 0):,} B")
                      for f in sorted(files, key=lambda x: x["path"])]
            items.append(Item(folder, folder, f"{len(leaves)} files", leaves))
    return items


def leaf_done(state: State, leaf: Leaf) -> bool:
    return state.is_done(leaf.key, "published")


def item_progress(state: State, item: Item) -> tuple[int, int, bool]:
    """(done, total, any_failed) across an item's leaves."""
    total = len(item.leaves)
    done = sum(1 for lf in item.leaves if leaf_done(state, lf))
    failed = any(state.get(lf.key) == "failed" for lf in item.leaves)
    return done, total, failed


def item_status(state: State, item: Item) -> str:
    """One of: migrated | partial | failed | pending | empty."""
    done, total, failed = item_progress(state, item)
    if total == 0:
        return "empty"
    if done == total:
        return "migrated"
    if failed:
        return "failed"
    if done == 0:
        return "pending"
    return "partial"


# =============================================================================
# Background worker — builds its own Context and runs phases off the UI thread.
# =============================================================================
class _SignalLogHandler(logging.Handler):
    """Bridges the 'artmig' logger to a Qt signal for the live console."""

    def __init__(self, emit_line) -> None:
        super().__init__()
        self._emit_line = emit_line
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s",
                                             datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit_line(self.format(record))
        except Exception:  # noqa: BLE001 - never let logging crash the worker
            pass


class Worker(QThread):
    """Runs one job: either an inventory refresh or a publish over N items.

    Publish iterates the chosen items one at a time (setting ctx.item_filter to a
    single id) so progress and cancellation are per-item.
    """

    log_line = Signal(str)
    item_started = Signal(str, str)         # (type, ident)
    item_finished = Signal(str, str, bool)  # (type, ident, ok)
    job_finished = Signal(bool, str)        # (ok, summary)

    def __init__(self, mode: str, type_name: str, idents: list[str],
                 dry_run: bool, config_path: Path) -> None:
        super().__init__()
        self.mode = mode                 # "publish" | "inventory"
        self.type_name = type_name
        self.idents = idents
        self.dry_run = dry_run
        self.config_path = config_path
        self._cancel = threading.Event()

    def cancel(self) -> None:
        self._cancel.set()

    # -- build a fresh Context owned entirely by this thread --------------------
    def _make_ctx(self) -> Context:
        cfg = load_config(self.config_path)
        art = Artifactory(cfg)
        gh = GitHub(cfg.github_slug, dry_run=self.dry_run)
        state = State(cfg.manifests_dir / "state.json")
        return Context(cfg=cfg, art=art, gh=gh, state=state, dry_run=self.dry_run)

    def run(self) -> None:  # noqa: D401 - QThread entry point
        log = get_logger()
        handler = _SignalLogHandler(self.log_line.emit)
        log.addHandler(handler)
        ok, summary = True, ""
        try:
            ctx = self._make_ctx()
            if self.mode == "inventory":
                ok, summary = self._run_inventory(ctx)
            else:
                ok, summary = self._run_publish(ctx)
        except Exception as exc:  # noqa: BLE001 - report, don't crash the GUI
            log.error("%s failed: %s", self.mode, exc)
            ok, summary = False, str(exc)
        finally:
            log.removeHandler(handler)
            self.job_finished.emit(ok, summary)

    def _run_inventory(self, ctx: Context) -> tuple[bool, str]:
        mod = MODULES[self.type_name]
        data_ = mod.inventory(ctx)
        path = ctx.write_inventory(self.type_name, data_)
        return True, f"inventory written to {path.name}"

    def _run_publish(self, ctx: Context) -> tuple[bool, str]:
        log = get_logger()
        mod = MODULES[self.type_name]
        failures = 0
        for ident in self.idents:
            if self._cancel.is_set():
                log.warning("cancelled before %s", ident)
                break
            self.item_started.emit(self.type_name, ident)
            ctx.item_filter = [ident]
            try:
                for phase in PUBLISH_PHASES:
                    if self._cancel.is_set():
                        break
                    getattr(mod, phase)(ctx)
                self.item_finished.emit(self.type_name, ident, True)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                log.error("%s:%s failed: %s", self.type_name, ident, exc)
                self.item_finished.emit(self.type_name, ident, False)
        done = len(self.idents) - failures
        mode = "dry-run" if self.dry_run else "publish"
        return failures == 0, f"{mode}: {done}/{len(self.idents)} ok"


# =============================================================================
# Main window
# =============================================================================
STATUS_TEXT = {
    "migrated": ("Migrated", DONE),
    "partial": ("In progress", ACCENT),
    "failed": ("Failed", FAIL),
    "pending": ("Not started", MUTED),
    "empty": ("No items", FAINT),
    "running": ("Running…", ACCENT),
}


class MainWindow(QMainWindow):
    def __init__(self, config_path: Path) -> None:
        super().__init__()
        self.config_path = config_path
        self.cfg = load_config(config_path)
        self.state = State(self.cfg.manifests_dir / "state.json")
        self.worker: Worker | None = None
        self.running_idents: set[str] = set()
        self._items_cache: dict[str, list[Item]] = {}

        self.setWindowTitle("artmig — migration console")
        self.resize(1120, 720)
        self._build_ui()
        self._reload_state()
        self._populate_categories()
        self.category_list.setCurrentRow(0)

    # ---- construction --------------------------------------------------------
    def _build_ui(self) -> None:
        self._build_toolbar()

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(self._build_rail())

        right = QSplitter(Qt.Vertical)
        right.addWidget(self._build_tree_panel())
        right.addWidget(self._build_console())
        right.setStretchFactor(0, 3)
        right.setStretchFactor(1, 1)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([250, 870])

        root.addWidget(split)
        self.setCentralWidget(central)
        self.setStyleSheet(_QSS)
        self.statusBar().showMessage("Ready")

    def _build_toolbar(self) -> None:
        tb = QToolBar()
        tb.setMovable(False)
        self.addToolBar(tb)

        self.overall_label = QLabel("overall  —")
        self.overall_label.setObjectName("overall")
        tb.addWidget(self.overall_label)

        self.overall_bar = QProgressBar()
        self.overall_bar.setFixedWidth(220)
        self.overall_bar.setTextVisible(False)
        tb.addWidget(self.overall_bar)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.dry_run_box = QCheckBox("dry run")
        self.dry_run_box.setChecked(True)
        self.dry_run_box.setToolTip("When on, migrations log actions without pushing to GitHub.")
        self.dry_run_box.toggled.connect(self._on_dry_toggled)
        tb.addWidget(self.dry_run_box)

        self.refresh_action = QAction("↻ Refresh inventory", self)
        self.refresh_action.triggered.connect(self._on_refresh)
        tb.addAction(self.refresh_action)

        self.migrate_all_action = QAction("Migrate pending in category", self)
        self.migrate_all_action.triggered.connect(self._on_migrate_all)
        tb.addAction(self.migrate_all_action)

    def _build_rail(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("rail")
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(8, 10, 8, 10)
        head = QLabel("CATEGORIES")
        head.setObjectName("railhead")
        lay.addWidget(head)
        self.category_list = QListWidget()
        self.category_list.setObjectName("catlist")
        self.category_list.currentRowChanged.connect(self._on_category_changed)
        lay.addWidget(self.category_list)
        return panel

    def _build_tree_panel(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)

        header = QWidget()
        header.setObjectName("mainhead")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(14, 10, 14, 10)
        self.main_title = QLabel("—")
        self.main_title.setObjectName("maintitle")
        hl.addWidget(self.main_title)
        hl.addStretch(1)
        lay.addWidget(header)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Item", "Items", "Status", ""])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(False)
        hv = self.tree.header()
        hv.setSectionResizeMode(0, QHeaderView.Stretch)
        hv.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hv.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        lay.addWidget(self.tree)
        return panel

    def _build_console(self) -> QWidget:
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        head = QLabel("ACTIVITY")
        head.setObjectName("consolehead")
        lay.addWidget(head)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setObjectName("console")
        self.console.setMaximumBlockCount(5000)
        # Pixel-sized to match the QSS (font-size: 12px). Mixing point- and
        # pixel-sized fonts makes Qt emit "setPointSize: Point size <= 0 (-1)".
        f = QFont("Consolas")
        f.setStyleHint(QFont.Monospace)
        f.setPixelSize(12)
        self.console.setFont(f)
        lay.addWidget(self.console)
        return panel

    # ---- data refresh --------------------------------------------------------
    def _reload_state(self) -> None:
        """Re-read state.json from disk (a worker may have just updated it)."""
        self.state = State(self.cfg.manifests_dir / "state.json")

    def _inventory_for(self, type_name: str) -> dict | None:
        try:
            return self.read_ctx().read_inventory(type_name)
        except FileNotFoundError:
            return None

    def read_ctx(self) -> Context:
        """A lightweight context for read-only rendering (no network calls made)."""
        art = Artifactory(self.cfg)
        gh = GitHub(self.cfg.github_slug, dry_run=True)
        return Context(cfg=self.cfg, art=art, gh=gh, state=self.state, dry_run=True)

    def _items_for(self, type_name: str) -> list[Item]:
        inv = self._inventory_for(type_name)
        if inv is None:
            return []
        return build_items(self.read_ctx(), type_name, inv)

    # ---- category rail -------------------------------------------------------
    def _populate_categories(self) -> None:
        self.category_list.blockSignals(True)
        self.category_list.clear()
        total_done = total_all = 0
        for type_name in MODULES:
            items = self._items_for(type_name)
            self._items_cache[type_name] = items
            done = all_ = 0
            for it in items:
                d, t, _ = item_progress(self.state, it)
                done += d
                all_ += t
            total_done += done
            total_all += all_
            label = self._category_label(type_name, items, done, all_)
            lw = QListWidgetItem(label)
            lw.setData(Qt.UserRole, type_name)
            self.category_list.addItem(lw)
        self.category_list.blockSignals(False)
        pct = int(100 * total_done / total_all) if total_all else 0
        self.overall_bar.setValue(pct)
        self.overall_label.setText(f"overall  {pct}%  ·  {total_done:,}/{total_all:,} items")

    def _category_label(self, type_name: str, items: list[Item], done: int, all_: int) -> str:
        if not items:
            return f"{type_name}\n   no inventory — Refresh"
        pct = int(100 * done / all_) if all_ else 0
        tag = "migrated" if done == all_ and all_ else f"{pct}%"
        return f"{type_name}   ({len(items)})\n   {done:,}/{all_:,} items · {tag}"

    # ---- tree ----------------------------------------------------------------
    def _current_type(self) -> str | None:
        it = self.category_list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _on_category_changed(self, _row: int) -> None:
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        type_name = self._current_type()
        self.tree.clear()
        if not type_name:
            return
        items = self._items_cache.get(type_name) or self._items_for(type_name)
        inv = self._inventory_for(type_name)
        if inv is None:
            self.main_title.setText(f"{type_name}  —  no inventory yet; click Refresh inventory")
            return
        self.main_title.setText(self._main_subtitle(type_name, inv, items))

        for it in items:
            node = QTreeWidgetItem([it.label, it.sublabel, "", ""])
            node.setData(0, Qt.UserRole, it.ident)
            # Taller row so the action button's text isn't clipped top/bottom.
            node.setSizeHint(0, QSize(0, _ROW_HEIGHT))
            self._style_item_node(node, it)
            self.tree.addTopLevelItem(node)
            # leaves
            for lf in it.leaves:
                st = self.state.get(lf.key)
                child = QTreeWidgetItem([lf.label, "", _leaf_status_text(st), ""])
                child.setForeground(0, QBrush(QColor(TEXT)))
                child.setForeground(1, QBrush(QColor(FAINT)))
                child.setToolTip(0, lf.detail)
                child.setForeground(2, QBrush(QColor(_leaf_status_color(st))))
                node.addChild(child)
            # action button in column 3
            self._add_action_button(node, type_name, it)

    def _main_subtitle(self, type_name: str, inv: dict, items: list[Item]) -> str:
        leaves = sum(len(it.leaves) for it in items)
        dest = {"pypi": "Releases + PEP 503", "docker": "ghcr.io",
                "npm": f"@{self.cfg.namespace}/*", "data": "Releases"}.get(type_name, "")
        return f"{type_name}   ·   {len(items)} items · {leaves} leaves   →   {dest}"

    def _style_item_node(self, node: QTreeWidgetItem, item: Item) -> None:
        running = item.ident in self.running_idents
        status = "running" if running else item_status(self.state, item)
        done, total, _ = item_progress(self.state, item)
        text, color = STATUS_TEXT[status]
        if status in ("partial", "failed") and total:
            text = f"{text} · {done}/{total}"
        node.setText(2, text)
        node.setForeground(0, QBrush(QColor(TEXT)))
        node.setForeground(1, QBrush(QColor(MUTED)))
        node.setForeground(2, QBrush(QColor(color)))
        bold = QFont()
        bold.setBold(True)
        bold.setPixelSize(13)   # pixel-sized (see console font note) to avoid the
        node.setFont(0, bold)   # point/pixel mix that warns "setPointSize(-1)"

    def _add_action_button(self, node: QTreeWidgetItem, type_name: str, item: Item) -> None:
        status = item_status(self.state, item)
        running = item.ident in self.running_idents
        btn = QPushButton()
        if running:
            btn.setText("Running…")
            btn.setEnabled(False)
        elif status == "migrated":
            btn.setText("Re-run")
            btn.setProperty("kind", "ghost")
        elif status == "failed":
            btn.setText("Retry")
            btn.setProperty("kind", "primary")
        else:
            btn.setText("Migrate")
            btn.setProperty("kind", "primary")
        btn.setEnabled(btn.isEnabled() and self.worker is None)
        btn.setMinimumHeight(_BTN_HEIGHT)
        btn.clicked.connect(lambda _=False, t=type_name, i=item.ident: self._start_publish(t, [i]))
        self.tree.setItemWidget(node, 3, btn)

    # ---- actions -------------------------------------------------------------
    def _on_dry_toggled(self, _checked: bool) -> None:
        self._update_console_banner()

    def _update_console_banner(self) -> None:
        mode = "DRY RUN — no changes pushed" if self.dry_run_box.isChecked() \
            else "LIVE — real push to GitHub/ghcr"
        self.statusBar().showMessage(mode)

    def _on_refresh(self) -> None:
        type_name = self._current_type()
        if not type_name or not self._guard_idle():
            return
        self._log(f"\n--- refreshing {type_name} inventory (crawls Artifactory) ---")
        self._start_worker(Worker("inventory", type_name, [], self.dry_run_box.isChecked(),
                                  self.config_path))

    def _on_migrate_all(self) -> None:
        type_name = self._current_type()
        if not type_name or not self._guard_idle():
            return
        items = self._items_cache.get(type_name, [])
        pending = [it.ident for it in items if item_status(self.state, it) != "migrated"]
        if not pending:
            QMessageBox.information(self, "Nothing to do",
                                    f"Every {type_name} item is already migrated.")
            return
        if not self._confirm_live(f"{len(pending)} {type_name} item(s)"):
            return
        self._start_publish(type_name, pending)

    def _start_publish(self, type_name: str, idents: list[str]) -> None:
        if not self._guard_idle():
            return
        if len(idents) == 1 and not self._confirm_live(f"{type_name}: {idents[0]}"):
            return
        self._log(f"\n--- {'dry-run ' if self.dry_run_box.isChecked() else ''}"
                  f"migrate {type_name}: {', '.join(idents)} ---")
        self.running_idents.update(idents)
        self._refresh_tree()
        self._start_worker(Worker("publish", type_name, idents,
                                  self.dry_run_box.isChecked(), self.config_path))

    # ---- worker plumbing -----------------------------------------------------
    def _guard_idle(self) -> bool:
        if self.worker is not None:
            QMessageBox.warning(self, "Busy", "A migration is already running.")
            return False
        return True

    def _confirm_live(self, what: str) -> bool:
        if self.dry_run_box.isChecked():
            return True
        resp = QMessageBox.question(
            self, "Confirm live migration",
            f"This will push {what} to GitHub for real.\n\nProceed?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return resp == QMessageBox.Yes

    def _start_worker(self, worker: Worker) -> None:
        self.worker = worker
        worker.log_line.connect(self._log)
        worker.item_finished.connect(self._on_item_finished)
        worker.job_finished.connect(self._on_job_finished)
        self.refresh_action.setEnabled(False)
        self.migrate_all_action.setEnabled(False)
        self._set_row_buttons_enabled(False)
        self._update_console_banner()
        worker.start()

    def _on_item_finished(self, _type: str, ident: str, ok: bool) -> None:
        self.running_idents.discard(ident)
        self._reload_state()
        self._refresh_tree()
        self._populate_categories_counts_only()

    def _on_job_finished(self, ok: bool, summary: str) -> None:
        self.running_idents.clear()
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        self._reload_state()
        self._populate_categories()
        self._refresh_tree()
        self.refresh_action.setEnabled(True)
        self.migrate_all_action.setEnabled(True)
        self._log(f"--- done: {summary} ---")
        self.statusBar().showMessage(summary)

    def _set_row_buttons_enabled(self, enabled: bool) -> None:
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            w = self.tree.itemWidget(node, 3)
            if isinstance(w, QPushButton):
                w.setEnabled(enabled)

    def _populate_categories_counts_only(self) -> None:
        # cheap refresh of the rail counts without rebuilding the whole tree
        self._populate_categories()
        # keep the current selection highlighted
        cur = self._current_type()
        if cur:
            for i in range(self.category_list.count()):
                if self.category_list.item(i).data(Qt.UserRole) == cur:
                    self.category_list.setCurrentRow(i)
                    break

    # ---- console -------------------------------------------------------------
    def _log(self, line: str) -> None:
        self.console.appendPlainText(line)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt override
        if self.worker is not None:
            self.worker.cancel()
            self.worker.wait(3000)
        super().closeEvent(event)


def _leaf_status_text(status: str | None) -> str:
    return {
        None: "not started",
        "pending": "pending",
        "downloaded": "downloaded",
        "published": "migrated",
        "verified": "verified",
        "failed": "failed",
        "skipped": "skipped",
    }.get(status, status or "not started")


def _leaf_status_color(status: str | None) -> str:
    if status in ("published", "verified"):
        return DONE
    if status == "failed":
        return FAIL
    if status in ("downloaded",):
        return ACCENT
    return MUTED


# ---- stylesheet (mirrors the mockup) ----------------------------------------
_QSS = f"""
QMainWindow, QWidget {{ background: {GROUND}; color: {TEXT};
    font-family: {MONO}; font-size: 12px; }}
QToolBar {{ background: {PANEL}; border: none; border-bottom: 1px solid {LINE};
    padding: 6px 10px; spacing: 12px; }}
QToolBar QToolButton {{ color: {TEXT}; background: transparent;
    border: 1px solid {LINE}; border-radius: 6px; padding: 5px 11px; }}
QToolBar QToolButton:hover {{ background: #16302e; }}
QToolBar QToolButton:disabled {{ color: {FAINT}; border-color: {PANEL2}; }}
QLabel#overall {{ color: {MUTED}; padding-right: 8px; }}
QProgressBar {{ background: #0c181a; border: 1px solid {LINE}; border-radius: 6px;
    height: 10px; }}
QProgressBar::chunk {{ background: {DONE}; border-radius: 5px; }}
QCheckBox {{ color: {ACCENT}; padding: 0 6px; }}

QFrame#rail {{ background: {PANEL2}; border-right: 1px solid {LINE}; }}
QLabel#railhead, QLabel#consolehead {{ color: {FAINT}; font-size: 10px;
    letter-spacing: 2px; padding: 4px 6px; }}
QListWidget#catlist {{ background: transparent; border: none; outline: none; }}
QListWidget#catlist::item {{ color: {TEXT}; padding: 10px 10px; border-radius: 8px;
    margin: 2px 2px; }}
QListWidget#catlist::item:selected {{ background: #16302e; color: {TEXT};
    border: 1px solid {LINE}; }}
QListWidget#catlist::item:hover {{ background: #142427; }}

QWidget#mainhead {{ background: {PANEL}; border-bottom: 1px solid {LINE}; }}
QLabel#maintitle {{ color: {TEXT}; font-size: 13px; font-weight: 600; }}

QTreeWidget {{ background: {PANEL2}; border: none; outline: none; }}
QTreeWidget::item {{ padding: 6px 4px; border-bottom: 1px solid #1d3436; }}
QTreeWidget::item:hover {{ background: #122325; }}
QTreeWidget::item:selected {{ background: #16302e; color: {TEXT}; }}
QHeaderView::section {{ background: {PANEL2}; color: {FAINT}; border: none;
    border-bottom: 1px solid {LINE}; padding: 7px 8px; font-size: 10px;
    letter-spacing: 1px; }}

QPlainTextEdit#console {{ background: #0b1517; color: {MUTED};
    border: none; border-top: 1px solid {LINE}; padding: 8px; }}

QPushButton {{ border-radius: 6px; padding: 4px 14px; font-size: 12px;
    border: 1px solid {LINE}; background: transparent; color: {TEXT}; }}
QPushButton[kind="primary"] {{ background: {ACCENT}; color: #231704;
    border-color: {ACCENT}; font-weight: 600; }}
QPushButton[kind="ghost"] {{ color: {MUTED}; }}
QPushButton:disabled {{ color: {FAINT}; border-color: {PANEL2}; background: transparent; }}
QStatusBar {{ background: {PANEL}; color: {MUTED}; border-top: 1px solid {LINE}; }}
QSplitter::handle {{ background: {LINE}; }}
"""


def _default_config_path() -> Path:
    """migration.yaml next to the repo root (parent of this package), else cwd."""
    candidate = Path(__file__).resolve().parent.parent / "migration.yaml"
    return candidate if candidate.exists() else Path("migration.yaml")


def _render_icon_pixmap(size: int) -> QPixmap:
    """Render the app mark at ``size`` px: an amber package on a petrol tile with
    a teal 'migrate' arrow. Drawn in a 256-unit space and scaled, so every size
    is crisp (the taskbar typically asks for 32px, the title bar for 16px)."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(size / 256.0, size / 256.0)

    # rounded petrol tile
    p.setPen(QPen(QColor(LINE), 10))
    p.setBrush(QColor(GROUND))
    p.drawRoundedRect(14, 14, 256 - 28, 256 - 28, 46, 46)

    # package / crate outline (amber)
    p.setPen(QPen(QColor(ACCENT), 16, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(Qt.NoBrush)
    bx, by, bw, bh = 58, 104, 140, 96
    p.drawRoundedRect(bx, by, bw, bh, 12, 12)
    p.drawLine(bx, by + 30, bx + bw, by + 30)                # lid seam
    p.drawLine(bx + bw // 2, by + 30, bx + bw // 2, by + bh)  # center seam

    # teal 'migrate' arrow rising out of the box
    p.setPen(QPen(QColor(DONE), 18, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    cx = bx + bw // 2
    p.drawLine(cx, 92, cx, 36)        # shaft
    p.drawLine(cx, 36, cx - 24, 60)   # left head
    p.drawLine(cx, 36, cx + 24, 60)   # right head

    p.end()
    return pm


def app_icon() -> QIcon:
    """The console icon with explicit representations at the sizes Windows asks
    for — a single large pixmap often leaves the taskbar showing the python.exe
    icon, so bake in the standard shell sizes."""
    icon = QIcon()
    for s in (16, 20, 24, 32, 40, 48, 64, 128, 256):
        icon.addPixmap(_render_icon_pixmap(s))
    return icon


# Bump the suffix if the taskbar shows a stale icon: Windows caches the taskbar
# icon per AppUserModelID, and an earlier build cached the python fallback under
# the old id. A fresh id is a new identity with no stale cache.
APP_USER_MODEL_ID = "idm.artmig.console.v2"


def _set_windows_app_id() -> None:
    """Give the process an explicit AppUserModelID so the taskbar groups the app
    under our identity (and uses our window icon) instead of python.exe. Must run
    before any window is created. Logs the result so launch issues are visible."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        fn = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [ctypes.c_wchar_p]
        fn.restype = ctypes.c_long  # HRESULT; 0 == S_OK
        hr = fn(APP_USER_MODEL_ID)
        exe = sys.executable
        log.debug("windows AppUserModelID=%s (hr=0x%08x) via %s", APP_USER_MODEL_ID,
                  hr & 0xFFFFFFFF, exe)
        if "WindowsApps" in exe:
            log.warning("running under the Microsoft Store Python (%s) — it overrides the "
                        "taskbar identity, so the custom icon may not show. Use a python.org "
                        "or venv interpreter instead.", exe)
    except Exception as exc:  # noqa: BLE001 - cosmetic only; never block startup
        log.warning("could not set AppUserModelID: %s", exc)


def main() -> None:
    config_path = _default_config_path()
    _set_windows_app_id()

    app = QApplication(sys.argv)
    app.setApplicationName("artmig console")
    icon = app_icon()
    app.setWindowIcon(icon)
    log.debug("app icon sizes: %s", [s.width() for s in icon.availableSizes()])
    win = MainWindow(config_path)
    win.setWindowIcon(icon)
    win._update_console_banner()
    win.show()
    # Re-assert the icon after the native window (and its taskbar button) exists —
    # some Windows builds grab the taskbar icon at this point, not before show().
    win.setWindowIcon(icon)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
