"""Convert a whole folder (or a mixed selection) at once.

The window lists every file type found ("23 × HEIC", "4 × MOV") with a sensible target already
chosen for each, so most of the time it's one click: Convert.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGridLayout, QHBoxLayout, QLabel, QPushButton,
                               QScrollArea, QVBoxLayout, QWidget)

from . import dispatch, media
from .util import UserError, category, human_size, output_override
from .window import GlassWindow

SKIP = "Skip"
MAX_FILES = 20_000
NAMES = {"image": "Images", "video": "Videos", "audio": "Audio", "model": "3D models", "dxf": "CAD drawings",
         "pdf": "PDFs", "docx": "Word", "text": "Text", "premiere": "Premiere", "ai": "Illustrator"}


def scan(roots: list[Path], recursive: bool) -> tuple["OrderedDict[str, list[Path]]", int]:
    """Group files by extension. Returns ({'.heic': [...], ...}, number of unsupported files)."""
    groups: dict[str, list[Path]] = {}
    other = 0
    n = 0

    def add(p: Path):
        nonlocal other, n
        if p.name.startswith(".~") or p.name.startswith("."):
            return
        if category(p) is None:
            other += 1
            return
        groups.setdefault(p.suffix.lower(), []).append(p)
        n += 1

    for r in roots:
        if r.is_file():
            add(r)
            continue
        if recursive:
            for dirpath, dirnames, files in os.walk(r):
                # never re-convert our own output folders
                dirnames[:] = [d for d in dirnames if not d.endswith("(converted)") and d != "DragShift"]
                for f in files:
                    add(Path(dirpath) / f)
                    if n >= MAX_FILES:
                        break
        else:
            for p in sorted(r.iterdir()):
                if p.is_file():
                    add(p)
    order = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    return OrderedDict(order), other


def convert_all(jobs: list[tuple[Path, str, str]], dest_for=None) -> tuple[list[Path], list[str]]:
    """Run every (file, category, target); keep going on errors. Returns (outputs, error lines)."""
    outs: list[Path] = []
    errors: list[str] = []
    ctx = media._ctx
    with output_override(dest_for) if dest_for else _null():
        for i, (p, cat, target) in enumerate(jobs):
            if media._cancelled():
                raise media.Cancelled()
            ctx.batch = (i, len(jobs))
            media._report(0.0)
            try:
                outs.extend(dispatch.convert_file(p, cat, target))
            except media.Cancelled:
                raise
            except UserError as e:
                errors.append(f"{p.name}: {e}")
            except Exception as e:  # noqa: BLE001
                errors.append(f"{p.name}: {e.__class__.__name__}: {e}")
    return outs, errors


class _null:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class BulkWindow(GlassWindow):
    """Pick a target per file type, then convert everything in one background job."""

    def __init__(self, paths: list[str], submit):
        super().__init__("Convert folder", size=(600, 560))
        self.roots = [Path(p) for p in paths]
        self.submit_fn = submit
        folder = self.roots[0] if len(self.roots) == 1 and self.roots[0].is_dir() else None
        self.base = folder or self.roots[0].parent
        head = QLabel(folder.name if folder else f"{len(self.roots)} selected items")
        head.setObjectName("big")
        self.body.addWidget(head)
        self.info = QLabel()
        self.info.setObjectName("dim")
        self.body.addWidget(self.info)

        opts = QHBoxLayout()
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(True)
        self.recursive.setVisible(any(r.is_dir() for r in self.roots))
        self.recursive.toggled.connect(self.rescan)
        opts.addWidget(self.recursive)
        opts.addStretch(1)
        opts.addWidget(QLabel("Save"))
        self.where = QComboBox()
        self.where.addItem(f"into “{self.base.name} (converted)”", "mirror")
        self.where.addItem("next to the originals", "next")
        opts.addWidget(self.where)
        self.body.addLayout(opts)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                                  "QScrollArea > QWidget > QWidget { background: transparent; }")
        self.inner = QWidget()
        self.grid = QGridLayout(self.inner)
        self.grid.setContentsMargins(0, 4, 6, 4)
        self.grid.setHorizontalSpacing(12)
        self.grid.setVerticalSpacing(8)
        self.scroll.setWidget(self.inner)
        self.body.addWidget(self.scroll, 1)

        quick = QHBoxLayout()
        q = QLabel("Quick:")
        q.setObjectName("dim")
        quick.addWidget(q)
        for label, fn in (("All images → JPG", lambda: self._all("image", "JPG")),
                          ("All images → PNG", lambda: self._all("image", "PNG")),
                          ("All videos → MP4", lambda: self._all("video", "MP4")),
                          ("All audio → MP3", lambda: self._all("audio", "MP3"))):
            b = QPushButton(label)
            b.clicked.connect(fn)
            quick.addWidget(b)
        quick.addStretch(1)
        self.body.addLayout(quick)

        foot = QHBoxLayout()
        self.summary = QLabel()
        self.summary.setObjectName("good")
        foot.addWidget(self.summary)
        foot.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.close)
        foot.addWidget(cancel)
        self.go = QPushButton("Convert")
        self.go.setObjectName("primary")
        self.go.clicked.connect(self.start)
        foot.addWidget(self.go)
        self.body.addLayout(foot)
        self.combos: dict[str, QComboBox] = {}
        self.rescan()

    # ------------------------------------------------------------ list
    def rescan(self):
        while self.grid.count():
            it = self.grid.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        self.groups, other = scan(self.roots, self.recursive.isChecked())
        self.combos = {}
        total = sum(len(v) for v in self.groups.values())
        size = sum(f.stat().st_size for v in self.groups.values() for f in v if f.exists())
        self.info.setText(f"{total} files · {human_size(size)}" + (f" · {other} other files are ignored" if other else ""))
        r = 0
        for ext, files in self.groups.items():
            cat = category(files[0])
            name = QLabel(f"{ext.lstrip('.').upper()}")
            name.setStyleSheet("font-weight: 700;")
            cnt = QLabel(f"{len(files)} × {NAMES.get(cat, cat)}")
            cnt.setObjectName("dim")
            arrow = QLabel("→")
            combo = QComboBox()
            combo.addItem(SKIP, None)
            for t in dispatch.targets_for(cat, ext):
                combo.addItem(_label(t), t)
            d = dispatch.default_target(cat, ext)
            if d:
                combo.setCurrentIndex(max(0, combo.findData(d)))
            combo.currentIndexChanged.connect(self._update)
            self.combos[ext] = combo
            self.grid.addWidget(name, r, 0)
            self.grid.addWidget(cnt, r, 1)
            self.grid.addWidget(arrow, r, 2)
            self.grid.addWidget(combo, r, 3)
            r += 1
        if not self.groups:
            none = QLabel("No files DragShift can convert in here.")
            none.setObjectName("dim")
            self.grid.addWidget(none, 0, 0, 1, 4)
        self.grid.setColumnStretch(1, 1)
        self.grid.setRowStretch(r, 1)
        self._update()

    def _all(self, cat: str, target: str):
        for ext, combo in self.combos.items():
            if category(self.groups[ext][0]) == cat:
                i = combo.findData(target)
                if i >= 0:
                    combo.setCurrentIndex(i)

    def jobs(self) -> list[tuple[Path, str, str]]:
        out = []
        for ext, combo in self.combos.items():
            t = combo.currentData()
            if t:
                out += [(f, category(f), t) for f in self.groups[ext]]
        return out

    def _update(self, *_):
        n = len(self.jobs())
        self.go.setEnabled(n > 0)
        self.go.setText(f"Convert {n} file{'s' if n != 1 else ''}" if n else "Convert")
        self.summary.setText("")

    # ------------------------------------------------------------ run
    def start(self):
        jobs = self.jobs()
        if not jobs:
            return
        base = self.base
        dest = None
        if self.where.currentData() == "mirror":
            root = base.parent / f"{base.name} (converted)"

            def dest(src: Path, root=root, base=base):
                try:
                    rel = src.parent.relative_to(base)
                except ValueError:
                    rel = Path()
                return root / rel
        srcs = [str(j[0]) for j in jobs]

        def work():
            outs, errors = convert_all(jobs, dest)
            if errors and not outs:
                raise UserError(f"{len(errors)} file(s) failed. First error: {errors[0]}")
            if errors:
                print("bulk errors:\n  " + "\n  ".join(errors), flush=True)
            return outs, (f"{len(errors)} failed (see log)" if errors else "")

        self.submit_fn("bulk:Folder", srcs, work, f"Converting {len(jobs)} files…", cancellable=True)
        self.close()


def _label(t: str) -> str:
    return {"HEVC": "H.265", "PRORES": "ProRes"}.get(t, t)
