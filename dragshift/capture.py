"""Screenshots: pick an area (or a whole screen), then copy, edit or record it.

The picker freezes every screen first, so menus and tooltips stay in the shot. Coordinates are
logical (Qt) per screen; `physical_rect` turns them into pixels for FFmpeg.
"""
from __future__ import annotations

import os
import random
import string
import sys
import time
from pathlib import Path

from PySide6.QtCore import QMimeData, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (QColor, QCursor, QDrag, QFont, QGuiApplication, QImage, QPainter, QPainterPath,
                           QPen, QPixmap, QScreen)
from PySide6.QtCore import QStandardPaths
from PySide6.QtWidgets import QApplication, QHBoxLayout, QPushButton, QWidget

from .util import human_size, ui_font

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
DEFAULT_NAME = "DragShift %Y-%m-%d at %H.%M.%S"


# ---------------------------------------------------------------- names and folders
def format_name(pattern: str | None, when: float | None = None) -> str:
    """strftime tokens plus %r (4 random letters). Characters Windows forbids are replaced."""
    pattern = (pattern or DEFAULT_NAME).strip() or DEFAULT_NAME
    rnd = "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(4))
    try:
        name = time.strftime(pattern.replace("%r", rnd), time.localtime(time.time() if when is None else when))
    except ValueError:
        name = time.strftime(DEFAULT_NAME, time.localtime(time.time() if when is None else when))
    for ch in '<>:"/\\|?*':
        name = name.replace(ch, "-")
    return name.strip(" .") or "Capture"


def capture_dir(custom: str = "", video: bool = False) -> Path:
    if custom:
        d = Path(custom).expanduser()
    else:
        loc = QStandardPaths.MoviesLocation if video else QStandardPaths.PicturesLocation
        base = QStandardPaths.writableLocation(loc) or str(Path.home())
        d = Path(base) / "DragShift"
    d.mkdir(parents=True, exist_ok=True)
    return d


def unique(d: Path, stem: str, ext: str) -> Path:
    p = d / f"{stem}{ext}"
    n = 2
    while p.exists():
        p = d / f"{stem} ({n}){ext}"
        n += 1
    return p


def physical_rect(screen: QScreen, local: QRect) -> QRect:
    """Pixels on the whole desktop (Windows/Linux) or within the screen (macOS, for a crop filter).
    Qt keeps every screen's origin in device pixels and scales only its extent."""
    dpr = screen.devicePixelRatio()
    x, y = round(local.x() * dpr), round(local.y() * dpr)
    w, h = round(local.width() * dpr), round(local.height() * dpr)
    if IS_MAC:
        return QRect(x, y, w, h)
    g = screen.geometry()
    return QRect(g.x() + x, g.y() + y, w, h)


def grab_screen(screen: QScreen) -> QPixmap:
    pm = screen.grabWindow(0)
    if pm.isNull():
        pm = QPixmap(screen.size() * screen.devicePixelRatio())
        pm.fill(Qt.black)
        pm.setDevicePixelRatio(screen.devicePixelRatio())
    return pm


def crop_pixmap(pm: QPixmap, local: QRect) -> QImage:
    dpr = pm.devicePixelRatio()
    r = QRect(round(local.x() * dpr), round(local.y() * dpr), round(local.width() * dpr), round(local.height() * dpr))
    img = pm.copy(r).toImage()
    img.setDevicePixelRatio(dpr)
    return img


# ---------------------------------------------------------------- area picker
class _Shade(QWidget):
    def __init__(self, picker: "AreaPicker", screen: QScreen, pm: QPixmap):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.picker, self.screen_, self.pm = picker, screen, pm
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.setGeometry(screen.geometry())
        self.a: QPoint | None = None
        self.b: QPoint | None = None
        self.mouse = self.mapFromGlobal(QCursor.pos())

    def sel(self) -> QRect | None:
        if self.a is None or self.b is None:
            return None
        return QRect(self.a, self.b).normalized()

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self.picker.cancel()
            return
        if e.button() == Qt.LeftButton:
            self.a = self.b = e.position().toPoint()
            self.picker.focus(self)
            self.update()

    def mouseMoveEvent(self, e):
        self.mouse = e.position().toPoint()
        if self.a is not None and e.buttons() & Qt.LeftButton:
            p = self.mouse
            if e.modifiers() & Qt.ShiftModifier:        # square
                d = max(abs(p.x() - self.a.x()), abs(p.y() - self.a.y()))
                p = QPoint(self.a.x() + (d if p.x() >= self.a.x() else -d), self.a.y() + (d if p.y() >= self.a.y() else -d))
            self.b = p
        self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self.a is None:
            return
        r = self.sel()
        if r is None or r.width() < 6 or r.height() < 6:
            r = self.rect()                               # a click takes the whole screen
        self.picker.done(self, r.intersected(self.rect()))

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.picker.cancel()
        elif e.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            r = self.sel()
            self.picker.done(self, r if r is not None and r.width() > 5 else self.rect())

    def paintEvent(self, e):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pm)
        r = self.sel()
        shade = QPainterPath()
        shade.addRect(QRectF(self.rect()))
        if r is not None:
            hole = QPainterPath()
            hole.addRect(QRectF(r))
            shade = shade.subtracted(hole)
        p.fillPath(shade, QColor(0, 0, 0, 105 if r is not None else 70))
        p.setRenderHint(QPainter.Antialiasing)
        if r is not None:
            p.setPen(QPen(QColor(255, 255, 255, 230), 1.2))
            p.drawRect(QRectF(r).adjusted(-0.5, -0.5, 0.5, 0.5))
            dpr = self.pm.devicePixelRatio()
            self._badge(p, f"{round(r.width() * dpr)} × {round(r.height() * dpr)}",
                        QPoint(r.left(), r.bottom() + 8 if r.bottom() + 40 < self.height() else r.top() - 34))
        else:
            m = self.mouse
            p.setPen(QPen(QColor(255, 255, 255, 120), 1, Qt.DashLine))
            p.drawLine(0, m.y(), self.width(), m.y())
            p.drawLine(m.x(), 0, m.x(), self.height())
            if self.picker.hint and self.rect().contains(m):
                self._badge(p, self.picker.hint, QPoint(self.width() // 2, 34), center=True)

    def _badge(self, p: QPainter, text: str, at: QPoint, center: bool = False):
        f = ui_font(13, True)
        p.setFont(f)
        w = p.fontMetrics().horizontalAdvance(text) + 22
        x = at.x() - w // 2 if center else min(max(4, at.x()), self.width() - w - 4)
        box = QRectF(x, at.y(), w, 28)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(28, 24, 38, 215))
        p.drawRoundedRect(box, 9, 9)
        p.setPen(QColor(255, 255, 255))
        p.drawText(box, Qt.AlignCenter, text)


class AreaPicker(QObject):
    """picked(screen, local QRect, frozen QPixmap of that screen); cancelled()."""
    picked = Signal(object, object, object)
    cancelled = Signal()

    def __init__(self, hint: str = "Drag to select · click = whole screen · Esc = cancel"):
        super().__init__()
        self.hint = hint
        self.shades: list[_Shade] = []
        self._closing = False

    def start(self):
        grabs = [(s, grab_screen(s)) for s in QGuiApplication.screens()]
        for s, pm in grabs:
            sh = _Shade(self, s, pm)
            self.shades.append(sh)
            sh.show()
            sh.raise_()
        under = QGuiApplication.screenAt(QCursor.pos())
        for sh in self.shades:
            if sh.screen_ is under:
                self.focus(sh)
        if IS_MAC:
            from . import platform_mac
            platform_mac.activate()

    def focus(self, sh):
        sh.activateWindow()
        sh.setFocus()

    def _close(self):
        self._closing = True
        for sh in self.shades:
            sh.close()
        self.shades = []

    def cancel(self):
        if self._closing:
            return
        self._close()
        self.cancelled.emit()

    def done(self, sh: _Shade, r: QRect):
        if self._closing:
            return
        screen, pm = sh.screen_, sh.pm
        self._close()
        self.picked.emit(screen, r, pm)


# ---------------------------------------------------------------- the card that pops up after a capture
class CaptureCard(QWidget):
    """Thumbnail bottom-left. Hover for actions, drag it into any app, click ✕ or wait to dismiss."""
    closed = Signal(object)
    W, H = 268, 178

    def __init__(self, thumb: QImage, caption: str, badge: str, actions: list[tuple[str, object]],
                 path_fn=None, image: QImage | None = None):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(self.W, self.H)
        self.caption, self.badge, self.path_fn, self.image = caption, badge, path_fn, image
        self.thumb = QPixmap.fromImage(thumb.scaled(QSize(self.W * 2, (self.H - 34) * 2), Qt.KeepAspectRatioByExpanding,
                                                    Qt.SmoothTransformation)) if thumb is not None and not thumb.isNull() else None
        self._press: QPoint | None = None
        row = QHBoxLayout()
        row.setContentsMargins(10, 10, 10, 0)
        row.setSpacing(6)
        self.buttons = []
        for label, fn in actions:
            b = QPushButton(label)
            b.setCursor(Qt.PointingHandCursor)
            b.setStyleSheet("QPushButton { background: rgba(20,18,28,0.82); color: white; border: 1px solid "
                            "rgba(255,255,255,0.35); border-radius: 11px; padding: 4px 10px; font-weight: 600; }"
                            "QPushButton:hover { background: rgba(255,90,120,0.9); }")
            b.clicked.connect(lambda _=False, fn=fn: self._act(fn))
            b.hide()
            row.addWidget(b)
            self.buttons.append(b)
        row.addStretch(1)
        x = QPushButton("✕")
        x.setFixedSize(24, 24)
        x.setStyleSheet("QPushButton { background: rgba(20,18,28,0.82); color: white; border-radius: 12px; border: none; }"
                        "QPushButton:hover { background: rgba(255,80,100,0.95); }")
        x.clicked.connect(self.close)
        x.hide()
        row.addWidget(x, 0, Qt.AlignTop)
        self.buttons.append(x)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        holder = QWidget()
        holder.setLayout(row)
        holder.setAttribute(Qt.WA_TranslucentBackground)
        lay.addWidget(holder, 0, Qt.AlignTop)
        self.life = QTimer(self, singleShot=True, interval=12000, timeout=self.close)
        self.life.start()

    def _act(self, fn):
        keep = fn()
        if keep is not True:
            self.close()

    def enterEvent(self, e):
        self.life.stop()
        for b in self.buttons:
            b.show()
        self.update()

    def leaveEvent(self, e):
        self.life.start(6000)
        for b in self.buttons:
            b.hide()
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.position().toPoint()

    def mouseMoveEvent(self, e):
        if self._press is None or (e.position().toPoint() - self._press).manhattanLength() < 8:
            return
        self._press = None
        path = self.path_fn() if self.path_fn else None
        md = QMimeData()
        if path:
            md.setUrls([QUrl.fromLocalFile(str(path))])
        if self.image is not None:
            md.setImageData(self.image)
        drag = QDrag(self)
        drag.setMimeData(md)
        if self.thumb is not None:
            drag.setPixmap(self.thumb.scaled(140, 90, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        if drag.exec(Qt.CopyAction | Qt.MoveAction) != Qt.IgnoreAction:
            self.close()

    def mouseReleaseEvent(self, e):
        self._press = None

    def closeEvent(self, e):
        self.closed.emit(self)
        super().closeEvent(e)

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        clip = QPainterPath()
        clip.addRoundedRect(r, 16, 16)
        p.setClipPath(clip)
        p.fillRect(r, QColor(24, 22, 34))
        if self.thumb is not None:
            t = QRectF(r.left(), r.top(), r.width(), r.height() - 34)
            src = QRectF(self.thumb.rect())
            k = max(t.width() / src.width(), t.height() / src.height())
            sw, sh = t.width() / k, t.height() / k
            p.drawPixmap(t, self.thumb, QRectF((src.width() - sw) / 2, (src.height() - sh) / 2, sw, sh))
        bar = QRectF(r.left(), r.bottom() - 34, r.width(), 34)
        p.fillRect(bar, QColor(30, 28, 52, 240))
        p.setClipping(False)
        x = bar.left() + 10
        if self.badge:
            f = ui_font(11, True)
            p.setFont(f)
            bw = p.fontMetrics().horizontalAdvance(self.badge) + 12
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255))
            p.drawRoundedRect(QRectF(x, bar.center().y() - 10, bw, 20), 5, 5)
            p.setPen(QColor(20, 20, 30))
            p.drawText(QRectF(x, bar.center().y() - 10, bw, 20), Qt.AlignCenter, self.badge)
            x += bw + 8
        p.setFont(ui_font(12, True))
        p.setPen(QColor(255, 255, 255))
        p.drawText(QRectF(x, bar.top(), bar.right() - x - 10, bar.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   self.caption)
        p.setPen(QPen(QColor(255, 255, 255, 70), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 16, 16)


class CardStack(QObject):
    def __init__(self):
        super().__init__()
        self.cards: list[CaptureCard] = []

    def add(self, card: CaptureCard):
        self.cards.append(card)
        card.closed.connect(self._closed)
        self._layout()
        card.show()

    def _closed(self, c):
        if c in self.cards:
            self.cards.remove(c)
        self._layout()

    def _layout(self):
        scr = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        y = scr.bottom() - 20
        for c in reversed(self.cards):
            y -= c.height()
            c.move(scr.left() + 20, y)
            y -= 12


def image_caption(img: QImage) -> str:
    return f"{img.width()} × {img.height()}"


def video_caption(path: Path, seconds: float) -> str:
    s = int(round(seconds))
    dur = f"{s}s" if s < 60 else f"{s // 60}:{s % 60:02d}"
    try:
        size = human_size(os.path.getsize(path))
    except OSError:
        size = ""
    return f"{dur}   {size}"
