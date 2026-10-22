"""Conversion history: a small JSON log plus the History window (open, show, remove, clear all)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout,
                               QWidget)

from .util import human_size
from .window import GlassWindow

MAX_ENTRIES = 500
_LOCK = threading.Lock()


def history_path() -> Path:
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "DragShift"
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DragShift"
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.json"


def load() -> list[dict]:
    try:
        data = json.loads(history_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(entries: list[dict]) -> None:
    p = history_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries[:MAX_ENTRIES], ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def record(result) -> None:
    """Add a finished job (report.Result) at the top of the history."""
    if not getattr(result, "outs", None):
        return
    entry = {
        "id": uuid.uuid4().hex,
        "time": time.time(),
        "title": result.title,
        "detail": result.detail,
        "good": result.good,
        "srcs": [str(s) for s in result.srcs],
        "outs": [str(o) for o in result.outs],
    }
    with _LOCK:
        entries = load()
        entries.insert(0, entry)
        _save(entries)


def remove(entry_id: str) -> None:
    with _LOCK:
        _save([e for e in load() if e.get("id") != entry_id])


def clear() -> None:
    with _LOCK:
        _save([])


def when(ts: float) -> str:
    d = time.time() - ts
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{int(d // 60)} min ago"
    lt, now = time.localtime(ts), time.localtime()
    if lt.tm_yday == now.tm_yday and lt.tm_year == now.tm_year:
        return time.strftime("today %H:%M", lt)
    if d < 2 * 86400 and (now.tm_yday - lt.tm_yday) % 366 == 1:
        return time.strftime("yesterday %H:%M", lt)
    return time.strftime("%d.%m.%Y %H:%M", lt)


# ---------------------------------------------------------------- window
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


class _Row(QWidget):
    removed = Signal(str)

    def __init__(self, e: dict):
        super().__init__()
        self.e = e
        self.setObjectName("histRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("QWidget#histRow { background: rgba(255,255,255,0.06); border-radius: 12px; }"
                           "QWidget#histRow:hover { background: rgba(255,255,255,0.11); }")
        outs = [Path(o) for o in e.get("outs", [])]
        exists = [o for o in outs if o.exists()]
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 8, 8)
        row.setSpacing(10)

        thumb = QLabel()
        thumb.setFixedSize(46, 46)
        thumb.setAlignment(Qt.AlignCenter)
        thumb.setStyleSheet("background: rgba(0,0,0,0.25); border-radius: 9px; font-size: 15pt;")
        img = next((o for o in exists if o.suffix.lower() in IMAGE_EXT), None)
        pm = QPixmap(str(img)) if img is not None and img.stat().st_size < 40_000_000 else QPixmap()
        if not pm.isNull():
            thumb.setPixmap(pm.scaled(46, 46, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                            .copy(0, 0, 46, 46))
        else:
            thumb.setText(_glyph(outs[0].suffix.lower() if outs else ""))
        row.addWidget(thumb)

        col = QVBoxLayout()
        col.setSpacing(1)
        t = QLabel(e.get("title", ""))
        t.setStyleSheet("font-weight: 600;")
        name = outs[0].name if len(outs) == 1 else f"{len(outs)} files · {outs[0].name} …" if outs else ""
        d = QLabel(f"{name}  ·  {when(e.get('time', 0))}")
        d.setObjectName("dim")
        d.setToolTip("\n".join(str(o) for o in outs[:20]))
        col.addWidget(t)
        col.addWidget(d)
        if not exists:
            gone = QLabel("file moved or deleted")
            gone.setObjectName("warn")
            col.addWidget(gone)
        row.addLayout(col, 1)

        def btn(text, tip, fn, enabled=True):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setEnabled(enabled)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(fn)
            row.addWidget(b)
            return b

        from .toasts import open_file, reveal
        btn("Open", "Open the result", lambda: open_file(exists[0]), bool(exists))
        btn("Folder", "Show in folder", lambda: reveal(exists[0]), bool(exists))
        x = btn("✕", "Remove from history (the file stays)", lambda: self.removed.emit(e["id"]))
        x.setObjectName("chrome")

    def matches(self, q: str) -> bool:
        q = q.lower()
        hay = " ".join([self.e.get("title", ""), *self.e.get("outs", []), *self.e.get("srcs", [])]).lower()
        return q in hay


def _glyph(ext: str) -> str:
    if ext in (".mp4", ".mov", ".mkv", ".webm", ".avi", ".gif"):
        return "▶"
    if ext in (".mp3", ".wav", ".flac", ".m4a", ".ogg", ".opus", ".aiff"):
        return "♪"
    if ext in (".stl", ".3mf", ".obj", ".ply", ".glb"):
        return "◆"
    if ext == ".pdf":
        return "▤"
    return "▣"


class HistoryWindow(GlassWindow):
    def __init__(self):
        super().__init__("History", size=(680, 640))
        top = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self._filter)
        top.addWidget(self.search, 1)
        self.count = QLabel()
        self.count.setObjectName("dim")
        top.addWidget(self.count)
        self.clear_btn = QPushButton("Clear all")
        self.clear_btn.setToolTip("Empty the history (your files stay where they are)")
        self.clear_btn.clicked.connect(self._clear_all)
        top.addWidget(self.clear_btn)
        self.body.addLayout(top)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                                  "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.inner = QWidget()
        self.list = QVBoxLayout(self.inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(6)
        self.scroll.setWidget(self.inner)
        self.body.addWidget(self.scroll, 1)
        self.rows: list[_Row] = []
        self._confirm = False
        self.reload()

    def reload(self):
        for r in self.rows:
            r.setParent(None)
            r.deleteLater()
        self.rows = []
        while self.list.count():
            it = self.list.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        entries = load()
        for e in entries:
            r = _Row(e)
            r.removed.connect(self._remove)
            self.rows.append(r)
            self.list.addWidget(r)
        if not entries:
            empty = QLabel("Nothing converted yet.\nDrag a file, press Shift, drop it on a slice.")
            empty.setAlignment(Qt.AlignCenter)
            empty.setObjectName("dim")
            self.list.addWidget(empty)
        self.list.addStretch(1)
        self.clear_btn.setEnabled(bool(entries))
        self._filter(self.search.text())

    def _filter(self, q: str):
        shown = 0
        for r in self.rows:
            ok = not q or r.matches(q)
            r.setVisible(ok)
            shown += ok
        total = len(self.rows)
        self.count.setText(f"{shown} of {total}" if q else (f"{total} item{'s' if total != 1 else ''}" if total else ""))

    def _remove(self, entry_id: str):
        remove(entry_id)
        for r in self.rows:
            if r.e.get("id") == entry_id:
                r.hide()
                self.rows.remove(r)
                r.deleteLater()
                break
        self._filter(self.search.text())
        if not self.rows:
            self.reload()

    def _clear_all(self):
        if not self._confirm:  # two clicks so nothing vanishes by accident
            self._confirm = True
            self.clear_btn.setText("Sure? Click again")
            QTimer.singleShot(3000, self._reset_confirm)
            return
        clear()
        self._reset_confirm()
        self.reload()

    def _reset_confirm(self):
        self._confirm = False
        self.clear_btn.setText("Clear all")


def total_size(entries: list[dict]) -> str:
    n = 0
    for e in entries:
        for o in e.get("outs", []):
            try:
                n += os.path.getsize(o)
            except OSError:
                pass
    return human_size(n)
