"""Screenshot editor: arrows, boxes, text, blur, counters, crop – then Copy, Save or Done."""
from __future__ import annotations

import math
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (QColor, QGuiApplication, QIcon, QImage, QKeySequence, QPainter, QPainterPath, QPen,
                           QPixmap, QPolygonF, QShortcut)
from PySide6.QtWidgets import (QButtonGroup, QColorDialog, QFileDialog, QGridLayout, QHBoxLayout, QLineEdit,
                               QMenu, QPushButton, QWidget, QWidgetAction)

from . import capture
from .tools import Shape, draw_shape
from .window import GlassWindow, ImageView

TOOLS = [  # key, tooltip, shortcut
    ("Crop", "Crop", "C"), ("Rect", "Rectangle", "R"), ("Filled", "Filled rectangle", "F"),
    ("Ellipse", "Ellipse", "O"), ("Line", "Line", "L"), ("Arrow", "Arrow", "A"), ("Text", "Text", "T"),
    ("Pixelate", "Pixelate (hide private info)", "X"), ("Spotlight", "Spotlight – dims everything else", "S"),
    ("Step", "Counter 1, 2, 3…", "N"), ("Pen", "Pen", "P"), ("Highlight", "Marker", "M")]
COLORS = ["#ff3b30", "#ff9500", "#ffcc00", "#34c759", "#0a84ff", "#af52de", "#ff2d92", "#ffffff", "#1c1c1e"]
WIDTHS = [2, 4, 7]


def tool_icon(name: str, color: str = "#ffffff") -> QIcon:
    s = 40
    pm = QPixmap(s, s)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(QColor(color), 3, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    c = QColor(color)
    if name == "Crop":
        p.drawPolyline(QPolygonF([QPointF(12, 6), QPointF(12, 28), QPointF(34, 28)]))
        p.drawPolyline(QPolygonF([QPointF(6, 12), QPointF(28, 12), QPointF(28, 34)]))
    elif name == "Rect":
        p.drawRoundedRect(QRectF(8, 10, 24, 20), 3, 3)
    elif name == "Filled":
        p.setBrush(c)
        p.drawRoundedRect(QRectF(8, 10, 24, 20), 3, 3)
    elif name == "Ellipse":
        p.drawEllipse(QRectF(8, 9, 24, 22))
    elif name == "Line":
        p.drawLine(QPointF(9, 31), QPointF(31, 9))
    elif name == "Arrow":
        p.drawLine(QPointF(10, 30), QPointF(28, 12))
        p.setBrush(c)
        p.drawPolygon(QPolygonF([QPointF(31, 9), QPointF(19, 12), QPointF(28, 21)]))
    elif name == "Text":
        f = p.font()
        f.setPixelSize(26)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 0, s, s), Qt.AlignCenter, "A")
    elif name == "Pixelate":
        p.setPen(Qt.NoPen)
        for i in range(3):
            for j in range(3):
                col = QColor(color)
                col.setAlpha(255 if (i + j) % 2 == 0 else 110)
                p.setBrush(col)
                p.drawRect(QRectF(8 + i * 8, 8 + j * 8, 8, 8))
    elif name == "Spotlight":
        p.drawRoundedRect(QRectF(6, 8, 28, 24), 4, 4)
        p.setBrush(c)
        p.drawRoundedRect(QRectF(13, 14, 14, 12), 2, 2)
    elif name == "Step":
        p.setBrush(c)
        p.setPen(Qt.NoPen)
        p.drawEllipse(QRectF(8, 8, 24, 24))
        f = p.font()
        f.setPixelSize(16)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor("#1c1c1e") if c.lightness() > 150 else QColor("#ffffff"))
        p.drawText(QRectF(8, 8, 24, 24), Qt.AlignCenter, "1")
    elif name == "Pen":
        p.drawLine(QPointF(10, 30), QPointF(28, 12))
        p.drawLine(QPointF(25, 9), QPointF(31, 15))
    elif name == "Highlight":
        col = QColor(color)
        col.setAlpha(150)
        p.setPen(QPen(col, 9, Qt.SolidLine, Qt.FlatCap))
        p.drawLine(QPointF(8, 24), QPointF(32, 24))
        p.setPen(pen)
        f = p.font()
        f.setPixelSize(17)
        f.setBold(True)
        p.setFont(f)
        p.drawText(QRectF(0, 2, s, 26), Qt.AlignCenter, "A")
    p.end()
    return QIcon(pm)


def dot_icon(color: str, w: int | None = None) -> QIcon:
    pm = QPixmap(40, 40)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    if w is None:
        p.setPen(QPen(QColor(255, 255, 255, 220), 2))
        p.setBrush(QColor(color))
        p.drawEllipse(QRectF(9, 9, 22, 22))
    else:
        p.setPen(QPen(QColor("#ffffff"), w * 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(9, 31), QPointF(31, 9))
    p.end()
    return QIcon(pm)


def render(p: QPainter, base: QImage, shapes: list[Shape], to_w, scale: float):
    """Draw shapes over `base` (already painted). to_w maps image → painter coordinates."""
    spots = [QRectF(to_w(sh.pts[0]), to_w(sh.pts[1])).normalized()
             for sh in shapes if sh.kind == "Spotlight" and len(sh.pts) == 2]
    if spots:            # dim the picture first, so arrows and text on top stay bright
        whole = QPainterPath()
        whole.addRect(QRectF(to_w(QPointF(0, 0)), to_w(QPointF(base.width(), base.height()))))
        for r in spots:
            hole = QPainterPath()
            hole.addRoundedRect(r, 8 * scale, 8 * scale)
            whole = whole.subtracted(hole)
        p.fillPath(whole, QColor(0, 0, 0, 140))
    for sh in shapes:
        if len(sh.pts) == 2 and sh.kind in ("Pixelate", "Filled", "Line", "Spotlight"):
            a, b = to_w(sh.pts[0]), to_w(sh.pts[1])
            r = QRectF(a, b).normalized()
            if sh.kind == "Pixelate":
                src = QRectF(sh.pts[0], sh.pts[1]).normalized().toRect().intersected(base.rect())
                if src.width() < 2 or src.height() < 2:
                    continue
                block = max(6, int(min(src.width(), src.height()) / 10))
                small = base.copy(src).scaled(max(1, src.width() // block), max(1, src.height() // block),
                                              Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                p.save()
                p.setRenderHint(QPainter.SmoothPixmapTransform, False)
                p.drawImage(QRectF(to_w(QPointF(src.topLeft())), to_w(QPointF(src.topLeft()) + QPointF(src.width(), src.height()))),
                            small)
                p.restore()
            elif sh.kind == "Filled":
                p.setPen(Qt.NoPen)
                p.setBrush(sh.color)
                p.drawRoundedRect(r, max(1.0, sh.width * scale), max(1.0, sh.width * scale))
            elif sh.kind == "Line":
                p.setPen(QPen(sh.color, max(1.0, sh.width * scale), Qt.SolidLine, Qt.RoundCap))
                p.drawLine(a, b)
            continue
        draw_shape(p, sh, to_w, scale)


def flatten(base: QImage, shapes: list[Shape]) -> QImage:
    out = base.convertToFormat(QImage.Format_ARGB32_Premultiplied)
    out.setDevicePixelRatio(1)
    p = QPainter(out)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    render(p, base, shapes, lambda q: QPointF(q), 1.0)
    p.end()
    out.setDevicePixelRatio(base.devicePixelRatio())
    return out


class SnapView(ImageView):
    def __init__(self, image: QImage, owner: "SnapEditor"):
        self.dpr = max(1.0, image.devicePixelRatio())
        super().__init__(image)
        self.owner = owner
        self.shapes: list[Shape] = []
        self.cur: Shape | None = None
        self.crop: tuple[QPointF, QPointF] | None = None
        self.editor: QLineEdit | None = None
        self.setCursor(Qt.CrossCursor)

    def set_image(self, image: QImage):
        image = QImage(image)
        image.setDevicePixelRatio(1)
        super().set_image(image)

    def target_rect(self) -> QRectF:
        if self.image.isNull():
            return QRectF()
        avail = QRectF(self.rect()).adjusted(8, 8, -8, -8)
        s = min(avail.width() / self.image.width(), avail.height() / self.image.height(), 1.0 / self.dpr)
        w, h = self.image.width() * s, self.image.height() * s
        return QRectF(avail.center().x() - w / 2, avail.center().y() - h / 2, w, h)

    def _commit_text(self):
        ed = self.editor
        if ed is None:
            return
        self.editor = None
        text = ed.text().strip()
        if text:
            self.owner.push()
            self.shapes.append(Shape("Text", ed.property("color"), ed.property("w"), [ed.property("pt")], text))
            self.update()
        ed.deleteLater()

    def _constrain(self, a: QPointF, b: QPointF, kind: str) -> QPointF:
        dx, dy = b.x() - a.x(), b.y() - a.y()
        if kind in ("Line", "Arrow"):
            ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
            d = math.hypot(dx, dy)
            return QPointF(a.x() + d * math.cos(ang), a.y() + d * math.sin(ang))
        d = max(abs(dx), abs(dy))
        return QPointF(a.x() + math.copysign(d, dx or 1), a.y() + math.copysign(d, dy or 1))

    def mousePressEvent(self, e):
        self._commit_text()
        if e.button() != Qt.LeftButton or not self.target_rect().adjusted(-20, -20, 20, 20).contains(e.position()):
            return
        o = self.owner
        p = self.clamp_image(self.to_image(e.position()))
        width = o.width / self.scale()
        if o.tool == "Text":
            ed = QLineEdit(self)
            ed.setPlaceholderText("Type, then Enter")
            ed.setProperty("pt", p)
            ed.setProperty("color", o.color)
            ed.setProperty("w", width * 1.6)
            ed.move(int(e.position().x()), int(e.position().y()) - 16)
            ed.resize(240, 30)
            ed.show()
            ed.setFocus()
            ed.returnPressed.connect(self._commit_text)
            self.editor = ed
            return
        if o.tool == "Step":
            o.push()
            n = sum(1 for s in self.shapes if s.kind == "Step") + 1
            self.shapes.append(Shape("Step", o.color, width * 1.6, [p], str(n)))
            self.update()
            return
        if o.tool == "Crop":
            self.crop = (p, p)
            self.update()
            return
        self.cur = Shape(o.tool, o.color, width, [p, p] if o.tool not in ("Pen", "Highlight") else [p])

    def mouseMoveEvent(self, e):
        p = self.clamp_image(self.to_image(e.position()))
        if self.crop is not None and e.buttons() & Qt.LeftButton:
            a = self.crop[0]
            self.crop = (a, self._constrain(a, p, "Crop") if e.modifiers() & Qt.ShiftModifier else p)
            self.update()
            return
        if not self.cur:
            return
        if self.cur.kind in ("Pen", "Highlight"):
            self.cur.pts.append(p)
        else:
            a = self.cur.pts[0]
            self.cur.pts[1] = self._constrain(a, p, self.cur.kind) if e.modifiers() & Qt.ShiftModifier else p
        self.update()

    def mouseReleaseEvent(self, e):
        if self.crop is not None:
            a, b = self.crop
            self.crop = None
            r = QRectF(a, b).normalized()
            if r.width() > 4 and r.height() > 4:
                self.owner.apply_crop(r.toRect())
            self.update()
            return
        if self.cur:
            sh, self.cur = self.cur, None
            if len(sh.pts) == 2 and (sh.pts[0] - sh.pts[1]).manhattanLength() < 3:
                self.update()
                return
            self.owner.push()
            self.shapes.append(sh)
            self.update()

    def paint_overlay(self, p, t):
        s = self.scale()
        render(p, self.image, self.shapes + ([self.cur] if self.cur else []), self.to_widget, s)
        if self.crop is not None:
            r = QRectF(self.to_widget(self.crop[0]), self.to_widget(self.crop[1])).normalized()
            dim = QPainterPath()
            dim.addRect(t)
            hole = QPainterPath()
            hole.addRect(r)
            p.fillPath(dim.subtracted(hole), QColor(0, 0, 0, 150))
            p.setPen(QPen(QColor(255, 255, 255), 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(r)


class SnapEditor(GlassWindow):
    """saved(path) after Save/Done; copied() after Copy."""
    saved = Signal(object)

    def __init__(self, image: QImage, settings=None, title: str = "Screenshot"):
        super().__init__(title, size=(980, 720))
        self.settings = settings
        self.tool, self.color, self.width = "Arrow", COLORS[0], WIDTHS[1]
        self.undo_stack: list[tuple[QImage, list[Shape]]] = []
        self.redo_stack: list[tuple[QImage, list[Shape]]] = []
        self.saved_path: Path | None = None

        bar = QHBoxLayout()
        bar.setSpacing(4)
        self.group = QButtonGroup(self)
        self.tool_buttons = {}
        for key, tip, sc in TOOLS:
            b = QPushButton()
            b.setIcon(tool_icon(key))
            b.setIconSize(QSize(22, 22))
            b.setCheckable(True)
            b.setToolTip(f"{tip}  ({sc})")
            b.setFixedSize(38, 34)
            b.setStyleSheet("QPushButton { border-radius: 9px; padding: 0; }")
            b.clicked.connect(lambda _=False, k=key: self.set_tool(k))
            self.group.addButton(b)
            bar.addWidget(b)
            self.tool_buttons[key] = b
            QShortcut(QKeySequence(sc), self, activated=lambda k=key: self.set_tool(k))
            if key in ("Crop", "Text", "Spotlight"):
                bar.addSpacing(8)
        bar.addSpacing(10)
        self.color_btn = QPushButton()
        self.color_btn.setFixedSize(38, 34)
        self.color_btn.setIconSize(QSize(26, 26))
        self.color_btn.setToolTip("Colour")
        self.color_btn.setStyleSheet("QPushButton { border-radius: 9px; padding: 0; }")
        self.color_btn.clicked.connect(self._color_menu)
        bar.addWidget(self.color_btn)
        self.width_btn = QPushButton()
        self.width_btn.setFixedSize(38, 34)
        self.width_btn.setIconSize(QSize(22, 22))
        self.width_btn.setToolTip("Line width (click to change)")
        self.width_btn.setStyleSheet("QPushButton { border-radius: 9px; padding: 0; }")
        self.width_btn.clicked.connect(self._next_width)
        bar.addWidget(self.width_btn)
        bar.addStretch(1)
        self.body.addLayout(bar)

        self.view = SnapView(image, self)
        self.body.addWidget(self.view, 1)

        foot = QHBoxLayout()
        for label, fn, tip in (("Undo", self.undo, "Ctrl+Z"), ("Redo", self.redo, "Ctrl+Y")):
            b = QPushButton(label)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            foot.addWidget(b)
        foot.addStretch(1)
        copy = QPushButton("Copy")
        copy.setToolTip("Copy to the clipboard (Ctrl+C)")
        copy.clicked.connect(self.copy)
        foot.addWidget(copy)
        save = QPushButton("Save as…")
        save.setToolTip("Ctrl+Shift+S")
        save.clicked.connect(self.save_as)
        foot.addWidget(save)
        done = QPushButton("Done")
        done.setObjectName("primary")
        done.setToolTip("Save to your capture folder + copy (Enter)")
        done.clicked.connect(self.done)
        foot.addWidget(done)
        self.body.addLayout(foot)

        QShortcut(QKeySequence.Undo, self, activated=self.undo)
        QShortcut(QKeySequence.Redo, self, activated=self.redo)
        QShortcut(QKeySequence("Ctrl+Y"), self, activated=self.redo)
        QShortcut(QKeySequence.Copy, self, activated=self.copy)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self.done)
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self.save_as)
        QShortcut(QKeySequence(Qt.Key_Return), self, activated=self._enter)
        QShortcut(QKeySequence(Qt.Key_Enter), self, activated=self._enter)
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.close)
        self.set_tool("Arrow")
        self._refresh_buttons()
        self._fit(image)

    def _fit(self, image: QImage):
        dpr = max(1.0, image.devicePixelRatio())
        w, h = image.width() / dpr + 60, image.height() / dpr + 150
        self.resize(int(min(max(w, 760), 1600)), int(min(max(h, 480), 1000)))

    # ---- state
    def push(self):
        self.undo_stack.append((self.view.image, list(self.view.shapes)))
        self.redo_stack.clear()

    def undo(self):
        self.view._commit_text()
        if self.undo_stack:
            self.redo_stack.append((self.view.image, list(self.view.shapes)))
            img, shapes = self.undo_stack.pop()
            self.view.set_image(img)
            self.view.shapes = shapes

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append((self.view.image, list(self.view.shapes)))
            img, shapes = self.redo_stack.pop()
            self.view.set_image(img)
            self.view.shapes = shapes

    def apply_crop(self, r):
        r = r.intersected(self.view.image.rect())
        if r.width() < 2 or r.height() < 2:
            return
        self.push()
        off = QPointF(r.left(), r.top())
        moved = []
        for sh in self.view.shapes:
            n = Shape(sh.kind, sh.color, sh.width, [q - off for q in sh.pts], sh.text)
            moved.append(n)
        self.view.shapes = moved
        self.view.set_image(self.view.image.copy(r))
        self.set_tool("Arrow")

    def set_tool(self, key: str):
        self.view._commit_text() if hasattr(self, "view") else None
        self.tool = key
        self.tool_buttons[key].setChecked(True)

    def _refresh_buttons(self):
        self.color_btn.setIcon(dot_icon(self.color))
        self.width_btn.setIcon(dot_icon(self.color, self.width))

    def _next_width(self):
        self.width = WIDTHS[(WIDTHS.index(self.width) + 1) % len(WIDTHS)] if self.width in WIDTHS else WIDTHS[1]
        self._refresh_buttons()

    def _color_menu(self):
        m = QMenu(self)
        holder = QWidget()
        g = QGridLayout(holder)
        g.setContentsMargins(8, 8, 8, 8)
        for i, c in enumerate(COLORS):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setStyleSheet(f"background: {c}; border: 2px solid {'#ffffff' if c == self.color else 'rgba(255,255,255,0.3)'};")
            b.clicked.connect(lambda _=False, c=c: (self._set_color(c), m.close()))
            g.addWidget(b, i // 5, i % 5)
        more = QPushButton("…")
        more.setFixedSize(26, 26)
        more.setStyleSheet("padding: 0; border-radius: 13px;")
        more.clicked.connect(lambda: (m.close(), self._any_color()))
        g.addWidget(more, 1, 4)
        act = QWidgetAction(m)
        act.setDefaultWidget(holder)
        m.addAction(act)
        m.exec(self.color_btn.mapToGlobal(self.color_btn.rect().bottomLeft()))

    def _any_color(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Colour")
        if c.isValid():
            self._set_color(c.name())

    def _set_color(self, c):
        self.color = c
        self._refresh_buttons()

    # ---- output
    def result(self) -> QImage:
        self.view._commit_text()
        img = flatten(self.view.image, self.view.shapes)
        img.setDevicePixelRatio(1)
        return img

    def copy(self):
        QGuiApplication.clipboard().setImage(self.result())
        self.title_label.setText("Copied ✓")

    def _settings(self, key, default=""):
        try:
            return self.settings.get(key) if self.settings else default
        except Exception:
            return default

    def save_to(self, path: Path) -> Path:
        img = self.result()
        ext = path.suffix.lower()
        if ext in (".jpg", ".jpeg"):
            img = img.convertToFormat(QImage.Format_RGB888)
            img.save(str(path), "JPG", 92)
        else:
            img.save(str(path), None)
        self.saved_path = path
        self.saved.emit(path)
        return path

    def default_path(self) -> Path:
        d = capture.capture_dir(self._settings("capture_dir", ""))
        return capture.unique(d, capture.format_name(self._settings("capture_name", capture.DEFAULT_NAME)), ".png")

    def save_as(self):
        f, _ = QFileDialog.getSaveFileName(self, "Save screenshot", str(self.default_path()),
                                           "PNG (*.png);;JPG (*.jpg);;WebP (*.webp)")
        if f:
            self.save_to(Path(f))
            self.close()

    def _enter(self):
        if self.view.editor is not None:
            self.view._commit_text()
            return
        self.done()

    def done(self):
        img = self.result()
        QGuiApplication.clipboard().setImage(img)
        self.save_to(self.default_path())
        self.close()
