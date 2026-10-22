"""A small floating glass button that stays on top. Drop files or a folder on it to open the menu.

Drag it anywhere with the mouse (the position is remembered), click it to pick files,
right-click for History, Settings or to hide it.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import QMenu, QWidget

from . import glass

SIZE = 76


class DropIcon(QWidget):
    dropped = Signal(list)          # local paths
    clicked = Signal()
    moved_to = Signal(QPoint)
    menu_requested = Signal(QPoint)

    def __init__(self):
        super().__init__(None)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint |
                            Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAcceptDrops(True)
        self.setFixedSize(SIZE, SIZE)
        self.setToolTip("DragShift — drop files or a folder here")
        self.hot = False       # a drag is over us
        self.hover = False
        self.phase = 0.0
        self._press: QPoint | None = None
        self._moved = False
        self.anim = QTimer(self, interval=16, timeout=self._tick)
        self.busy = False

    # ------------------------------------------------------------ placement
    def place(self, pos: QPoint | None):
        scr = QGuiApplication.primaryScreen().availableGeometry()
        if pos is None or not any(s.availableGeometry().contains(pos + QPoint(SIZE // 2, SIZE // 2))
                                  for s in QGuiApplication.screens()):
            pos = QPoint(scr.right() - SIZE - 24, scr.bottom() - SIZE - 120)
        self.move(pos)

    def _tick(self):
        self.phase = (self.phase + 3) % 360
        self.update()

    # ------------------------------------------------------------ drag & drop
    @staticmethod
    def _paths(md) -> list[str]:
        import os
        return [os.path.normpath(u.toLocalFile()) for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e):
        if self._paths(e.mimeData()):
            e.acceptProposedAction()
            self.hot = True
            self.anim.start()
            self.update()

    def dragMoveEvent(self, e):
        e.acceptProposedAction()

    def dragLeaveEvent(self, e):
        self.hot = False
        self.anim.stop()
        self.update()

    def dropEvent(self, e):
        paths = self._paths(e.mimeData())
        self.hot = False
        self.anim.stop()
        self.update()
        if paths:
            e.acceptProposedAction()
            # let the drop finish in Explorer before the menu takes focus
            QTimer.singleShot(30, lambda: self.dropped.emit(paths))

    # ------------------------------------------------------------ mouse: move / click / menu
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._press = e.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._moved = False
        elif e.button() == Qt.RightButton:
            self.menu_requested.emit(e.globalPosition().toPoint())

    def mouseMoveEvent(self, e):
        if self._press is not None and e.buttons() & Qt.LeftButton:
            new = e.globalPosition().toPoint() - self._press
            if (new - self.pos()).manhattanLength() > 3 or self._moved:
                self._moved = True
                self.move(new)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.LeftButton:
            if self._moved:
                self.moved_to.emit(self.pos())
            else:
                self.clicked.emit()
        self._press = None

    def enterEvent(self, e):
        self.hover = True
        self.update()

    def leaveEvent(self, e):
        self.hover = False
        self.update()

    # ------------------------------------------------------------ paint
    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(SIZE / 2, SIZE / 2)
        r = SIZE / 2 - 6 + (3 if self.hot else 0) * (0.5 + 0.5 * math.sin(math.radians(self.phase * 2)))
        g = QRadialGradient(c - QPointF(r * 0.3, r * 0.4), r * 1.5)
        a = 235 if (self.hot or self.hover) else 205
        g.setColorAt(0, QColor(70, 62, 104, a))
        g.setColorAt(1, QColor(28, 22, 44, a))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, r, r)
        # highlight along the top rim
        p.setPen(QPen(QColor(255, 255, 255, 70), 1.2))
        p.drawArc(QRectF(c.x() - r + 3, c.y() - r + 3, 2 * r - 6, 2 * r - 6), 30 * 16, 120 * 16)
        # iridescent ring
        p.setPen(glass.iridescent_pen(c, 3.2 if self.hot else 2.4, 255 if self.hot else 200, angle=-self.phase))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(c, r - 1.5, r - 1.5)
        # glyph: a down arrow into a tray
        col = QColor(255, 255, 255, 240 if (self.hot or self.hover) else 210)
        pen = QPen(col, 2.6, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
        p.setPen(pen)
        s = r * 0.42
        top, mid = c.y() - s, c.y() + s * 0.25
        p.drawLine(QPointF(c.x(), top), QPointF(c.x(), mid))
        p.drawLine(QPointF(c.x() - s * 0.45, mid - s * 0.45), QPointF(c.x(), mid))
        p.drawLine(QPointF(c.x() + s * 0.45, mid - s * 0.45), QPointF(c.x(), mid))
        tray_y = c.y() + s * 0.7
        p.drawLine(QPointF(c.x() - s, tray_y - s * 0.35), QPointF(c.x() - s, tray_y))
        p.drawLine(QPointF(c.x() - s, tray_y), QPointF(c.x() + s, tray_y))
        p.drawLine(QPointF(c.x() + s, tray_y), QPointF(c.x() + s, tray_y - s * 0.35))


def context_menu(parent_menu_style: str, actions: list[tuple[str, object]]) -> QMenu:
    m = QMenu()
    m.setStyleSheet(parent_menu_style)
    for label, fn in actions:
        if label == "-":
            m.addSeparator()
        else:
            m.addAction(label, fn)
    return m


def cursor_pos() -> QPoint:
    return QCursor.pos()
