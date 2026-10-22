"""Windows for video tools that need a choice: "Fit size" (make a video smaller than N MB)."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QButtonGroup, QDoubleSpinBox, QHBoxLayout, QLabel, QPushButton

from . import media
from .tools import ToolWindow, section
from .util import UserError, file_size, human_size


class FitSizeWindow(ToolWindow):
    tool_name = "Fit size"
    PRESETS = [("8 MB", 8, "Discord"), ("10 MB", 10, ""), ("16 MB", 16, "WhatsApp"), ("25 MB", 25, "E-mail"),
               ("50 MB", 50, ""), ("100 MB", 100, "")]

    def __init__(self, paths, notify):
        super().__init__("Fit video to a file size", paths, notify, size=(600, 360))
        self.infos = []
        for p in self.paths:
            try:
                self.infos.append(media.probe(p))
            except UserError:
                self.infos.append(None)
        i0 = self.infos[0]
        if i0 is not None and i0.duration:
            w, h = i0.display_size
            self.subtitle.setText(f"{self.subtitle.text()}  ·  {w}×{h}  ·  {media.fmt_duration(i0.duration)}")
        self.body.addWidget(section("Make each video smaller than"))
        row = QHBoxLayout()
        self.grp = QButtonGroup(self)
        for i, (label, mb, hint) in enumerate(self.PRESETS):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setToolTip(hint)
            self.grp.addButton(b, i)
            row.addWidget(b)
        row.addSpacing(8)
        self.custom = QDoubleSpinBox()
        self.custom.setRange(0.5, 100000)
        self.custom.setDecimals(1)
        self.custom.setSuffix(" MB")
        self.custom.setValue(25)
        self.custom.setFixedWidth(110)
        row.addWidget(self.custom)
        row.addStretch(1)
        self.body.addLayout(row)
        self.est = QLabel()
        self.est.setObjectName("big")
        self.body.addWidget(self.est)
        self.note = QLabel()
        self.note.setObjectName("dim")
        self.note.setWordWrap(True)
        self.body.addWidget(self.note)
        self.body.addStretch(1)
        self.body.addLayout(self.footer("Shrink" if len(self.paths) == 1 else f"Shrink {len(self.paths)} videos",
                                        self.save))
        self.grp.idClicked.connect(lambda i: self.custom.setValue(self.PRESETS[i][1]))
        self.custom.valueChanged.connect(self._changed)
        self.grp.button(3).setChecked(True)
        self._changed()

    def _changed(self, *_):
        mb = self.custom.value()
        for i, (_, v, _) in enumerate(self.PRESETS):
            if abs(v - mb) < 1e-6:
                self.grp.button(i).setChecked(True)
                break
        else:
            b = self.grp.checkedButton()
            if b:
                self.grp.setExclusive(False)
                b.setChecked(False)
                self.grp.setExclusive(True)
        target = int(mb * 1_000_000)
        i0 = self.infos[0]
        size0 = file_size(self.path)
        self.est.setText(f"{human_size(size0)}  →  under {mb:g} MB")
        if i0 is None:
            self.note.setText("")
            return
        try:
            vk, ak, scale = media.plan_fit(i0, target)
        except UserError as e:
            self.note.setText(str(e))
            self.note.setObjectName("warn")
            self.primary_btn.setEnabled(False)
        else:
            if size0 <= target and len(self.paths) == 1:
                self.note.setText("This video is already smaller than that.")
            else:
                res = f", scaled to {scale}p" if scale else ""
                self.note.setText(f"H.264 MP4 at {vk} kbps{res}, audio {ak} kbps · two passes for an exact size")
            self.note.setObjectName("dim")
            self.primary_btn.setEnabled(True)
        self.note.style().unpolish(self.note)
        self.note.style().polish(self.note)

    def save(self):
        target = int(self.custom.value() * 1_000_000)
        paths = list(self.paths)
        self.run(lambda: media.run_batch(paths, lambda p: media.fit_size(p, target)), "Fitting to size…",
                 f"under {self.custom.value():g} MB")
