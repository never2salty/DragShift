"""System-wide keyboard shortcuts (they work while any other app has focus).

Windows: RegisterHotKey on a hidden window. macOS: an NSEvent global monitor (needs the
Accessibility permission DragShift already asks for). Elsewhere: not available.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QObject, Qt
from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import QWidget

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x8, 0x4000
WM_HOTKEY = 0x0312

_SPECIAL_VK = {Qt.Key_Print: 0x2C, Qt.Key_Space: 0x20, Qt.Key_Insert: 0x2D, Qt.Key_Pause: 0x13,
               Qt.Key_Home: 0x24, Qt.Key_End: 0x23, Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
               Qt.Key_Escape: 0x1B, Qt.Key_Tab: 0x09}
# macOS virtual key codes (ANSI layout)
_MAC_KEYS = {"A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9, "B": 11, "Q": 12,
             "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
             "9": 25, "7": 26, "8": 28, "0": 29, "O": 31, "U": 32, "I": 34, "P": 35, "L": 37, "J": 38, "K": 40,
             "N": 45, "M": 46, "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97, "F7": 98,
             "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111, "SPACE": 49}


def parse(seq: str) -> tuple[int, int] | None:
    """'Ctrl+Shift+1' → (Qt modifiers, Qt key)."""
    ks = QKeySequence(seq or "")
    if ks.isEmpty():
        return None
    c = ks[0]
    return int(c.keyboardModifiers().value), int(c.key().value)


def win_combo(seq: str) -> tuple[int, int] | None:
    """(MOD_* flags, virtual key) for RegisterHotKey."""
    pk = parse(seq)
    if not pk:
        return None
    mods, key = pk
    m = MOD_NOREPEAT
    if mods & Qt.ControlModifier.value:
        m |= MOD_CONTROL
    if mods & Qt.ShiftModifier.value:
        m |= MOD_SHIFT
    if mods & Qt.AltModifier.value:
        m |= MOD_ALT
    if mods & Qt.MetaModifier.value:
        m |= MOD_WIN
    if Qt.Key_0.value <= key <= Qt.Key_9.value or Qt.Key_A.value <= key <= Qt.Key_Z.value:
        vk = key                      # same codes as ASCII
    elif Qt.Key_F1.value <= key <= Qt.Key_F24.value:
        vk = 0x70 + key - Qt.Key_F1.value
    else:
        vk = _SPECIAL_VK.get(Qt.Key(key))
        if vk is None:
            return None
    return m, vk


class _Sink(QWidget):
    def __init__(self, owner):
        super().__init__()
        self.owner = owner

    def nativeEvent(self, etype, message):
        if IS_WIN:
            try:
                from ctypes import wintypes
                msg = wintypes.MSG.from_address(int(message))
                if msg.message == WM_HOTKEY:
                    self.owner._fire(int(msg.wParam))
                    return True, 0
            except Exception:
                pass
        return False, 0


class HotkeyManager(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.callbacks: dict[int, callable] = {}
        self.names: dict[int, str] = {}
        self._next = 0xB000
        self._sink = None
        self._mac_monitor = None
        self._mac_combos: dict[tuple[int, int], int] = {}

    @property
    def supported(self) -> bool:
        return IS_WIN or IS_MAC

    def _fire(self, hid: int):
        cb = self.callbacks.get(hid)
        if cb:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, cb)

    def clear(self):
        if IS_WIN and self._sink is not None:
            import ctypes
            hwnd = int(self._sink.winId())
            for hid in self.callbacks:
                ctypes.windll.user32.UnregisterHotKey(hwnd, hid)
        self.callbacks.clear()
        self.names.clear()
        self._mac_combos.clear()

    def register(self, seq: str, callback) -> bool:
        """True when the shortcut is active. False: empty, unsupported key, or taken by another app."""
        if not seq:
            return True
        hid = self._next
        self._next += 1
        if IS_WIN:
            combo = win_combo(seq)
            if combo is None:
                return False
            import ctypes
            if self._sink is None:
                self._sink = _Sink(self)
                self._sink.setAttribute(Qt.WA_DontShowOnScreen)
            if not ctypes.windll.user32.RegisterHotKey(int(self._sink.winId()), hid, combo[0], combo[1]):
                return False
        elif IS_MAC:
            if not self._mac_register(seq, hid):
                return False
        else:
            return False
        self.callbacks[hid] = callback
        self.names[hid] = seq
        return True

    # ---- macOS
    def _mac_register(self, seq: str, hid: int) -> bool:
        pk = parse(seq)
        if not pk:
            return False
        mods, key = pk
        name = QKeySequence(key).toString().upper()
        code = _MAC_KEYS.get(name)
        if code is None:
            return False
        try:
            import AppKit
        except Exception:
            return False
        flags = 0
        # Qt on macOS swaps Ctrl/Meta: Qt "Ctrl" is the ⌘ key, Qt "Meta" is the Control key
        if mods & Qt.ControlModifier.value:
            flags |= AppKit.NSEventModifierFlagCommand
        if mods & Qt.MetaModifier.value:
            flags |= AppKit.NSEventModifierFlagControl
        if mods & Qt.ShiftModifier.value:
            flags |= AppKit.NSEventModifierFlagShift
        if mods & Qt.AltModifier.value:
            flags |= AppKit.NSEventModifierFlagOption
        self._mac_combos[(code, flags)] = hid
        if self._mac_monitor is None:
            mask = (AppKit.NSEventModifierFlagCommand | AppKit.NSEventModifierFlagControl |
                    AppKit.NSEventModifierFlagShift | AppKit.NSEventModifierFlagOption)

            def handler(ev):
                k = (int(ev.keyCode()), int(ev.modifierFlags()) & mask)
                h = self._mac_combos.get(k)
                if h is not None:
                    self._fire(h)
                    return None
                return ev
            self._mac_monitor = (
                AppKit.NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, handler),
                AppKit.NSEvent.addLocalMonitorForEventsMatchingMask_handler_(AppKit.NSEventMaskKeyDown, handler))
        return True
