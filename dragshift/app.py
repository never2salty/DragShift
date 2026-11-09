"""DragShift: Shift+drag any file to convert or edit it from a radial liquid-glass menu."""
from __future__ import annotations

import json
import os
import sys
import time
import traceback
from pathlib import Path

from PySide6.QtCore import QDir, QObject, QPointF, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import (QAction, QColor, QCursor, QDesktopServices, QGuiApplication, QIcon, QPainter,
                           QPixmap)
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QFileDialog, QMenu, QSystemTrayIcon, QWidget

import threading

from . import (__version__, adobe, bulk, convert, dispatch, glass, history, imaging, media, model3d, report,
               tools, updater)
from .dropzone import DropIcon
from .radial import Item, RadialMenu
from .settings import Settings, SettingsWindow, WelcomeWindow, TRIGGERS
from .toasts import ToastStack
from .trigger import DragShiftWatcher
from .util import UserError, category, file_size, human_size, keep, release

APP_NAME = "DragShift"
TIP_URL = "https://cash.app/$AtikaKadesha"
VERSION = __version__
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
from .handoff import ipc_name
IPC_NAME = ipc_name()

GLYPHS = {"Compress": "⇲", "Metadata": "ⓘ", "Redact": "▮", "Crop": "⌗", "Edit": "☼",
          "Annotate": "✎", "Add BG": "▣", "Resize": "⤢", "Split": "✂", "Rotate": "⟳",
          "Merge": "⧉", "Protect": "⚿", "Extract": "⧈",
          "1080p": "⤓", "720p": "⤓", "Mute": "⊘", "Thumbnail": "▦", "Screenshots": "◎", "Normalize": "≋", "Trim silence": "⇥",
          "Mono": "◉", "Fit size": "⇲", "Repair": "✚", "Simplify": "◇", "Center": "⊕"}

HINTS = {
    "Compress": "Shrink file size", "Metadata": "View · remove GPS", "Redact": "Hide private info",
    "Crop": "Trim to a ratio", "Edit": "Light · colour · rotate", "Annotate": "Arrows · text · steps",
    "Add BG": "Gradient backdrop", "Resize": "Change dimensions", "Split": "One PDF per page",
    "Rotate": "Turn pages 90°", "Merge": "Join into one PDF", "Protect": "Add a password",
    "Extract": "Save embedded images",
    "1080p": "Downscale · H.264 MP4", "720p": "Downscale · H.264 MP4", "Mute": "Remove the audio",
    "Thumbnail": "Save a frame as JPG", "Screenshots": "Pick frames · save PNG/JPG", "Normalize": "Loudness −14 LUFS", "Trim silence": "Cut start/end silence",
    "Mono": "Stereo → mono",
}
VIDEO_HINTS = {"Compress": "Smaller H.264 MP4", "Rotate": "Turn 90° clockwise", "Fit size": "Under 8/16/25 MB…"}
MODEL_HINTS = {"Repair": "Fix normals · fill holes", "Simplify": "Half the triangles", "Center": "Middle of the bed, z = 0"}
FORMAT_HINTS = {"JPG": "Small, universal", "PNG": "Lossless · transparency", "WEBP": "Smallest for web",
                "HEIC": "iPhone format", "TIFF": "Print · archival", "AVIF": "Next-gen, tiny",
                "BMP": "Uncompressed", "PDF": "Document", "ICO": "Windows icon", "GIF": "Legacy web",
                "DOCX": "Editable Word", "TXT": "Plain text",
                # video
                "MP4": "H.264 · plays everywhere", "HEVC": "H.265 MP4 · half the size",
                "PRORES": "ProRes 422 · for editing", "MOV": "QuickTime · no re-encode",
                "MKV": "Matroska · no re-encode", "WEBM": "VP9 · for the web",
                # audio
                "MP3": "320 kbps · universal", "WAV": "Uncompressed", "FLAC": "Lossless, smaller",
                "M4A": "AAC 256 kbps · Apple", "ALAC": "Apple Lossless", "OGG": "Vorbis · open",
                "OPUS": "Best for voice/web", "AIFF": "Uncompressed · Mac"}
MODEL_FORMAT_HINTS = {"STL": "Every slicer", "3MF": "Bambu · Prusa · Cura", "OBJ": "Blender · games",
                      "PLY": "Scans · point data", "GLB": "Web · AR · 3D viewer", "PNG": "Preview picture",
                      "PDF": "Print the drawing", "SVG": "Vector · laser cutter"}
VIDEO_FORMAT_HINTS = {"GIF": "First 30 s · 15 fps", "MP3": "Extract audio · 320k", "WAV": "Extract audio"}
LABELS = {"HEVC": "H.265", "PRORES": "ProRes"}

IMAGE_TOOLS = ["Compress", "Resize", "Crop", "Edit", "Annotate", "Redact", "Add BG", "Metadata"]
PDF_TOOLS = ["Compress", "Split", "Rotate", "Protect", "Extract", "Metadata"]


# ---------------------------------------------------------------- background jobs
class _Sig(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)
    cancelled = Signal()


class Job(QRunnable):
    def __init__(self, action: str, srcs: list, fn, extra: str = ""):
        super().__init__()
        self.action, self.srcs, self.fn, self.extra = action, srcs, fn, extra
        self.sig = _Sig()
        self.cancel = threading.Event()

    def run(self):
        try:
            with media.job_context(lambda frac, text: self.sig.progress.emit(text), self.cancel):
                res = self.fn() or []
            extra = self.extra
            if isinstance(res, tuple):
                res, more = res
                extra = more or extra
            self.sig.done.emit(report.summarize(self.action, self.srcs, list(res), extra))
        except media.Cancelled:
            self.sig.cancelled.emit()
        except UserError as e:
            self.sig.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.sig.failed.emit(friendly_error(e))


def friendly_error(e: Exception) -> str:
    msg = str(e) or e.__class__.__name__
    low = msg.lower()
    if isinstance(e, PermissionError) or "permission denied" in low:
        return "No permission to write next to this file. Try a folder you own, or set Settings → Save results → subfolder."
    if "cannot identify image" in low:
        return "This file doesn't look like an image DragShift can read."
    if isinstance(e, FileNotFoundError):
        return "The file was moved or deleted."
    return msg


def needs_bulk(paths: list[str]) -> bool:
    """A folder, or several files of different kinds, goes to the folder window."""
    if any(os.path.isdir(p) for p in paths):
        return True
    cats = {category(p) for p in paths}
    return len(paths) > 1 and len(cats) > 1


# ---------------------------------------------------------------- icon
def make_tray_icon() -> QIcon:
    """macOS menu bar wants a monochrome 'template' image; Windows gets the colour icon."""
    if not IS_MAC:
        return make_icon()
    pm = QPixmap(44, 44)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    from PySide6.QtGui import QPen
    p.setPen(QPen(QColor(0, 0, 0), 4.5))
    p.drawEllipse(QPointF(22, 22), 14, 14)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(0, 0, 0))
    p.drawEllipse(QPointF(22, 22), 5, 5)
    for a in range(0, 360, 45):  # petal gaps
        import math
        x, y = 22 + 14 * math.cos(math.radians(a + 22.5)), 22 + 14 * math.sin(math.radians(a + 22.5))
        p.setCompositionMode(QPainter.CompositionMode_Clear)
        p.drawEllipse(QPointF(x, y), 2.2, 2.2)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
    p.end()
    pm.setDevicePixelRatio(2.0)
    icon = QIcon(pm)
    icon.setIsMask(True)
    return icon


def make_icon() -> QIcon:
    ico = Path(__file__).with_name("assets") / "dragshift.ico"
    if ico.exists():
        return QIcon(str(ico))
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(glass.iridescent_pen(QPointF(32, 32), 7, 255))
    p.drawEllipse(QPointF(32, 32), 24, 24)
    p.end()
    return QIcon(pm)


# ---------------------------------------------------------------- the app
class DragShiftApp(QObject):
    def __init__(self, qapp: QApplication):
        super().__init__()
        self.qapp = qapp
        self.settings = Settings()
        self.settings.apply()
        self.pool = QThreadPool.globalInstance()
        self.windows: list[QWidget] = []
        self.toasts = ToastStack()
        self.menu = RadialMenu(self.build_rings, self.describe)
        self.menu.activated.connect(self.run_item)

        # the watcher only manages menus it opened (drag mode); click-mode menus (tray, right-click,
        # Send To) must not be closed just because no mouse button is held
        self.watcher = DragShiftWatcher(lambda: self.menu.isVisible() and not self.menu.click_mode,
                                        lambda: self.menu.got_drag, self)
        self.watcher.set_key(self.settings.get("trigger"))
        self.watcher.show_menu.connect(lambda pos: self.menu.popup(pos))
        self.watcher.toggle_ring.connect(self.menu.next_ring)
        self.watcher.hide_menu.connect(self.menu.close_menu)
        self.watcher.start()

        # files handed over by "Send to", the right-click menu, or a second launch
        self._pending: list[str] = []
        self._pending_timer = QTimer(self, singleShot=True, interval=280, timeout=self._flush_pending)
        self.server = QLocalServer(self)
        QLocalServer.removeServer(IPC_NAME)
        self.server.listen(IPC_NAME)
        self.server.newConnection.connect(self._ipc)

        self.tray = QSystemTrayIcon(make_tray_icon(), self)
        self.tray.setToolTip(f"{APP_NAME} — press {self.trigger_label()} while dragging a file")
        m = QMenu()
        m.setStyleSheet(glass.QSS)
        self.act_enabled = QAction("Enabled", m, checkable=True, checked=True)
        self.act_enabled.toggled.connect(lambda on: setattr(self.watcher, "enabled", on))
        m.addAction(self.act_enabled)
        m.addAction("Open a file…", self.open_dialog)
        m.addAction("Convert a folder…", self.open_folder_dialog)
        m.addAction("History…", self.open_history)
        self.act_drop = QAction("Show drop icon", m, checkable=True, checked=self.settings.get("drop_icon"))
        self.act_drop.toggled.connect(self.set_drop_icon)
        m.addAction(self.act_drop)
        m.addSeparator()
        m.addAction("Settings…", self.open_settings)
        m.addAction("Check for updates…", lambda: self.check_updates(manual=True))
        m.addAction("How to use", self.show_welcome)
        m.addSeparator()
        m.addAction(f"Quit {APP_NAME}", self.qapp.quit)
        self.tray_menu = m
        self.tray.setContextMenu(m)
        self.tray.activated.connect(self._tray_click)
        self.tray.show()

        # floating drop icon
        self.drop_icon: DropIcon | None = None
        if self.settings.get("drop_icon"):
            self.set_drop_icon(True)
        # quiet update check once a day
        if self.settings.get("auto_update") and updater.can_self_update():
            last = float(self.settings.get("last_update_check") or 0)
            if time.time() - last > 20 * 3600:
                QTimer.singleShot(8000, lambda: self.check_updates(manual=False))

    def trigger_label(self) -> str:
        return dict(TRIGGERS).get(self.settings.get("trigger"), "Shift")

    # ------------------------------------------------------------ IPC
    def _ipc(self):
        sock = self.server.nextPendingConnection()
        if sock is None:
            return
        buf = bytearray()
        done = {"v": False}

        def consume():
            if done["v"]:
                return
            try:
                buf.extend(bytes(sock.readAll()))
            except RuntimeError:
                pass
            try:
                data = json.loads(buf.decode("utf-8"))
            except Exception:
                return  # incomplete: wait for more
            done["v"] = True
            files = [f for f in data.get("files", []) if os.path.exists(f)]
            print(f"IPC: {len(files)} file(s) handed over", flush=True)
            if files:
                self.queue_files(files)
            elif data.get("show"):
                self.toasts.info("DragShift is already running",
                                 f"Drag any file and press {self.trigger_label()}. It lives in the tray.")
            sock.disconnectFromServer()
            sock.deleteLater()

        # Windows pipes: the sender may already be gone, so read on every signal + a fallback timer
        sock.readyRead.connect(consume)
        sock.disconnected.connect(consume)
        for ms in (0, 50, 200, 600):
            QTimer.singleShot(ms, consume)

    def queue_files(self, files: list[str]):
        """Explorer's right-click menu launches once per file: gather them, then show one menu."""
        for f in files:
            if f not in self._pending:
                self._pending.append(f)
        self._pending_timer.start()

    def _flush_pending(self):
        files, self._pending = self._pending, []
        if files:
            self.open_paths(files)

    def open_paths(self, files: list[str]):
        """Files from the drop icon, right-click, Send to: one kind → the menu, folders/mixed → bulk."""
        if needs_bulk(files):
            self.open_bulk(files)
        else:
            self.show_click_menu(files)

    # ------------------------------------------------------------ menu contents
    def describe(self, paths: list[str]) -> str:
        if len(paths) > 1:
            return f"{len(paths)} files"
        return human_size(file_size(paths[0]))

    def build_rings(self, paths: list[str]) -> dict[str, list[Item]]:
        if needs_bulk(paths):
            n_dirs = sum(os.path.isdir(p) for p in paths)
            hint = "Pick a format per type" if n_dirs else f"{len(paths)} mixed files"
            return {"Folder": [Item("Convert all", "⧉", key="bulk:all", hint=hint)]}
        cat = category(paths[0])
        if cat is None:
            return {"Unsupported": []}
        ext = Path(paths[0]).suffix
        same = [p for p in paths if category(p) == cat]
        convert_items = []
        targets = dispatch.targets_for(cat, ext)
        for t in targets:
            hint = (VIDEO_FORMAT_HINTS.get(t) if cat == "video" else None) or \
                (MODEL_FORMAT_HINTS.get(t) if cat in ("model", "dxf") else None) or FORMAT_HINTS.get(t, "")
            if cat == "premiere":
                hint = "Version → 1 · safe copy"
            if cat in ("video", "audio", "model", "dxf", "premiere", "ai"):
                convert_items.append(Item(LABELS.get(t, t), key=f"convert:{t}", hint=hint))
                continue
            if cat == "image" and t == "PDF" and len(same) > 1:
                hint = f"{len(same)} images → 1 PDF"
            convert_items.append(Item(t, key=f"convert:{t}", hint=hint))
        rings: dict[str, list[Item]] = {"Convert": convert_items}
        if cat == "image":
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in IMAGE_TOOLS]
        elif cat == "pdf":
            names = PDF_TOOLS[:3] + (["Merge"] if len(same) > 1 else []) + PDF_TOOLS[3:]
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in names]
        elif cat == "video":
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=VIDEO_HINTS.get(n) or HINTS[n])
                              for n in media.VIDEO_TOOLS]
        elif cat == "audio":
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=HINTS[n]) for n in media.AUDIO_TOOLS]
        elif cat == "model":
            rings["Tools"] = [Item(n, GLYPHS[n], key=f"tool:{n}", hint=MODEL_HINTS[n]) for n in model3d.MODEL_TOOLS]
        return rings

    # ------------------------------------------------------------ actions
    def run_item(self, item: Item, paths: list[str]):
        kind, name = item.key.split(":", 1)
        if kind == "bulk":
            self.open_bulk(paths)
            return
        cat = category(paths[0])
        paths = [p for p in paths if category(p) == cat]
        if cat == "video" and name == "Screenshots":
            from .framegrab import make_window
            try:
                self.open_window(make_window(paths, self.notify))
            except UserError as e:
                self.toasts.error("Can't open this video", str(e))
            return
        if cat == "video" and name == "Fit size":
            from .mediatools import FitSizeWindow
            self.open_window(FitSizeWindow(paths, self.notify))
            return
        if cat in ("model", "dxf", "premiere", "ai"):
            label = LABELS.get(name, name)
            if kind == "convert":
                self.submit(f"convert:{label}", paths,
                            lambda: media.run_batch(paths, lambda p: dispatch.convert_file(p, cat, name)),
                            "Rendering…" if name == "PNG" else f"Converting to {label}…")
            else:
                self.submit(f"tool:{name}", paths, lambda: media.run_batch(paths, lambda p: model3d.tool(p, name)),
                            {"Repair": "Repairing…", "Simplify": "Simplifying…", "Center": "Centering…"}[name])
            return
        if cat in ("video", "audio"):
            label = LABELS.get(name, name)
            if kind == "convert":
                self.submit(f"convert:{label}", paths,
                            lambda: media.run_batch(paths, lambda p: media.convert(p, cat, name)),
                            f"Converting to {label}…", cancellable=True)
            else:
                verbs = {"Compress": "Compressing…", "Fit size": "Fitting to size…", "1080p": "Scaling to 1080p…", "720p": "Scaling to 720p…",
                         "Rotate": "Rotating…", "Mute": "Removing audio…", "Thumbnail": "Grabbing a frame…",
                         "Normalize": "Normalizing loudness…", "Trim silence": "Trimming silence…",
                         "Mono": "Mixing to mono…"}
                self.submit(f"tool:{name}", paths, lambda: media.run_batch(paths, lambda p: media.tool(p, name)),
                            verbs.get(name, "Working…"), cancellable=True)
            return
        if kind == "convert":
            if cat == "image" and name == "PDF" and len(paths) > 1:
                self.submit(f"convert:{name}", paths, lambda: convert.images_to_single_pdf(paths),
                            f"Combining {len(paths)} images…")
            else:
                verb = f"Converting to {name}…"
                self.submit(f"convert:{name}", paths,
                            lambda: media.run_batch(paths, lambda p: convert.convert(p, cat, name)), verb,
                            cancellable=len(paths) > 1)
            return
        if cat == "pdf":
            if name == "Merge":
                self.submit("tool:Merge", paths, lambda: [imaging.merge_pdfs(paths)], "Merging…")
            elif name == "Metadata":
                self.open_window(tools.MetadataWindow(paths[0], self.notify, is_pdf=True))
            elif name == "Protect":
                self.open_window(tools.ProtectWindow(paths[0], self.notify))
            else:
                fns = {"Compress": lambda p: [imaging.compress_pdf(p)], "Split": imaging.split_pdf,
                       "Rotate": lambda p: [imaging.rotate_pdf(p)], "Extract": imaging.extract_pdf_images}
                f = fns[name]
                self.submit(f"tool:{name}", paths, lambda: [o for p in paths for o in f(p)],
                            {"Compress": "Compressing…", "Split": "Splitting…", "Rotate": "Rotating…",
                             "Extract": "Extracting images…"}[name])
            return
        windows = {"Crop": tools.CropWindow, "Add BG": tools.AddBgWindow, "Annotate": tools.AnnotateWindow,
                   "Redact": tools.RedactWindow, "Edit": tools.EditWindow, "Metadata": tools.MetadataWindow}
        try:
            if name == "Compress":
                self.open_window(tools.CompressWindow(paths, self.notify))
            elif name == "Resize":
                self.open_window(tools.ResizeWindow(paths, self.notify))
            else:
                self.open_window(windows[name](paths[0], self.notify))
        except UserError as e:
            self.toasts.error(f"Couldn't open {name}", str(e))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.toasts.error(f"Couldn't open {name}", friendly_error(e))

    def open_window(self, w: QWidget):
        if hasattr(w, "submit"):
            w.submit = self.submit
        self.windows.append(w)
        w.destroyed.connect(lambda *_: self.windows.remove(w) if w in self.windows else None)
        if hasattr(w, "present"):
            w.present()
        else:
            w.show()

    def submit(self, action: str, srcs: list, fn, working: str = "Working…", extra: str = "",
               cancellable: bool = False):
        """Run in the background. A spinner card appears only if it takes > 150 ms."""
        if not cancellable and srcs and category(srcs[0]) in ("video", "audio"):
            cancellable = True
        state = {"card": None, "finished": False, "progress": ""}
        names = Path(srcs[0]).name if len(srcs) == 1 else f"{len(srcs)} files"
        job = Job(action, srcs, fn, extra)

        def show_spinner():
            if not state["finished"]:
                state["card"] = self.toasts.new()
                detail = f"{state['progress']} · {names}" if state["progress"] else names
                state["card"].working(working, detail, on_cancel=job.cancel.set if cancellable else None)
                # closed with ✕ while working: the job carries on, the result gets a fresh card
                state["card"].closed.connect(lambda *_: state.update(card=None))

        def progress(text):
            state["progress"] = text
            if state["card"] is not None and not state["finished"]:
                state["card"].set_progress(f"{text} · {names}")

        QTimer.singleShot(150, show_spinner)

        def done(r):
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.done(r)
            self.remember(r)

        def failed(msg):
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.error("Couldn't finish", msg)

        def cancelled():
            release(job)
            state["finished"] = True
            card = state["card"] or self.toasts.new()
            card.info("Cancelled", "Nothing was saved", 2500)

        job.sig.done.connect(done)
        job.sig.failed.connect(failed)
        job.sig.progress.connect(progress)
        job.sig.cancelled.connect(cancelled)
        self.pool.start(keep(job))

    def notify(self, action: str, srcs: list, outs: list, extra: str = ""):
        """Called by tool windows after they save."""
        r = report.summarize(action, srcs, outs, extra)
        self.toasts.new().done(r)
        self.remember(r)

    def remember(self, r):
        if not self.settings.get("history"):
            return
        try:
            history.record(r)
        except Exception:
            traceback.print_exc()
        w = next((w for w in self.windows if isinstance(w, history.HistoryWindow)), None)
        if w is not None:
            w.reload()

    # ------------------------------------------------------------ history, bulk, drop icon, updates
    def open_history(self):
        w = next((w for w in self.windows if isinstance(w, history.HistoryWindow)), None)
        if w is not None:
            w.reload()
            w.raise_()
            w.activateWindow()
            return
        self.open_window(history.HistoryWindow())

    def open_bulk(self, paths: list[str]):
        try:
            self.open_window(bulk.BulkWindow(paths, self.submit))
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self.toasts.error("Couldn't open the folder", friendly_error(e))

    def open_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(None, "Choose a folder to convert", QDir.homePath())
        if d:
            self.open_bulk([d])

    def set_drop_icon(self, on: bool):
        self.settings.set("drop_icon", bool(on))
        if self.act_drop.isChecked() != bool(on):
            self.act_drop.setChecked(bool(on))
        if on and self.drop_icon is None:
            ic = DropIcon()
            pos = self.settings.get("drop_icon_pos")
            from PySide6.QtCore import QPoint
            try:
                x, y = (int(v) for v in str(pos).split(","))
                ic.place(QPoint(x, y))
            except Exception:
                ic.place(None)
            ic.dropped.connect(self._icon_dropped)
            ic.clicked.connect(self.open_dialog)
            ic.moved_to.connect(lambda p: self.settings.set("drop_icon_pos", f"{p.x()},{p.y()}"))
            ic.menu_requested.connect(self._icon_menu)
            ic.show()
            self.drop_icon = ic
        elif not on and self.drop_icon is not None:
            self.drop_icon.close()
            self.drop_icon.deleteLater()
            self.drop_icon = None

    def _icon_dropped(self, files: list[str]):
        files = [f for f in files if os.path.exists(f)]
        if not files:
            return
        if needs_bulk(files):
            self.open_bulk(files)
            return
        # open the menu right next to the icon so the choice is one short move away
        g = self.drop_icon.frameGeometry()
        from PySide6.QtCore import QPoint
        scr = (QGuiApplication.screenAt(g.center()) or QGuiApplication.primaryScreen()).availableGeometry()
        x = g.left() - 210 if g.center().x() > scr.center().x() else g.right() + 210
        y = min(max(g.center().y(), scr.top() + 210), scr.bottom() - 210)
        self.menu.popup(QPoint(x, y), files)
        self.menu.activateWindow()

    def _icon_menu(self, pos):
        m = QMenu()
        m.setStyleSheet(glass.QSS)
        m.addAction("Open a file…", self.open_dialog)
        m.addAction("Convert a folder…", self.open_folder_dialog)
        m.addAction("History…", self.open_history)
        m.addSeparator()
        m.addAction("Settings…", self.open_settings)
        m.addAction("Hide drop icon", lambda: self.set_drop_icon(False))
        m.exec(pos)

    def check_updates(self, manual: bool = True):
        self.settings.set("last_update_check", str(time.time()))
        state = {}

        def work():
            state["rel"] = updater.latest()
            return []

        job = Job("update:check", [], work)

        def checked(_r):
            release(job)
            rel = state.get("rel")
            if rel is None:
                return
            if updater.newer(rel.version):
                self._offer_update(rel)
            elif manual:
                self.toasts.info("You're up to date", f"DragShift {VERSION} is the latest version.")

        def failed(msg):
            release(job)
            if manual:
                self.toasts.error("Couldn't check for updates", msg)

        job.sig.done.connect(checked)
        job.sig.failed.connect(failed)
        self.pool.start(keep(job))

    def _offer_update(self, rel):
        from .settings import UpdateWindow
        w = UpdateWindow(rel, VERSION)
        w.install.connect(lambda: self._install_update(rel))
        self.open_window(w)

    def _install_update(self, rel):
        if not updater.can_self_update():
            QDesktopServices.openUrl(QUrl(rel.page))
            return
        state = {"card": self.toasts.new()}
        state["card"].working(f"Downloading DragShift {rel.version}…", "starting…")
        holder = {}

        def work():
            holder["file"] = updater.fetch_update(rel)
            return []

        job = Job("update:get", [], work)
        state["card"].on_cancel = job.cancel.set
        job.sig.progress.connect(lambda t: state["card"].set_progress(t))

        def done(_r):
            release(job)
            state["card"].info("Installing update…", "DragShift restarts in a moment", 4000)
            try:
                updater.apply_update(holder["file"])
            except Exception as e:  # noqa: BLE001
                state["card"].error("Update failed", str(e))
                return
            QTimer.singleShot(600, self.qapp.quit)

        def failed(msg):
            release(job)
            state["card"].error("Update failed", msg)

        job.sig.done.connect(done)
        job.sig.failed.connect(failed)
        job.sig.cancelled.connect(lambda: (release(job), state["card"].info("Update cancelled", "", 2000)))
        self.pool.start(keep(job))

    # ------------------------------------------------------------ misc
    def _tray_click(self, reason):
        # macOS: a click on the menu-bar icon shows the menu; Windows: click opens a file
        if not IS_MAC and reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger):
            self.open_dialog()

    def open_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(None, "Choose file(s) for DragShift", QDir.homePath())
        if files:
            self.show_click_menu(files)

    def show_click_menu(self, files: list[str]):
        g = (QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()).availableGeometry()
        pos = QCursor.pos() if g.contains(QCursor.pos()) else g.center()
        self.menu.popup(pos, files)
        self.menu.activateWindow()

    def open_settings(self):
        w = SettingsWindow(self.settings, check_updates=lambda: self.check_updates(manual=True))
        w.changed.connect(self._settings_changed)
        self.open_window(w)

    def _settings_changed(self):
        on = bool(self.settings.get("drop_icon"))
        if on != (self.drop_icon is not None):
            self.set_drop_icon(on)
        self.watcher.set_key(self.settings.get("trigger"))
        self.tray.setToolTip(f"{APP_NAME} — press {self.trigger_label()} while dragging a file")

    def show_welcome(self):
        self.open_window(WelcomeWindow(self.settings, self.trigger_label()))


class DragShiftQApplication(QApplication):
    """macOS delivers files from Finder's "Open With", the Dock and `open -a` as FileOpen
    events (not argv)."""
    file_opened = Signal(str)

    def __init__(self, argv):
        super().__init__(argv)
        self.early_files: list[str] = []

    def event(self, e):
        from PySide6.QtCore import QEvent
        if e.type() == QEvent.FileOpen:
            f = e.file()
            if f:
                self.early_files.append(f)
                self.file_opened.emit(f)
            return True
        return super().event(e)


def mac_setup(qapp):
    # fonts: util.ui_family picks a real installed family. (Substituting the private
    # ".AppleSystemUIFont" made labels draw nothing on macOS 26.)
    try:
        from . import platform_mac
        platform_mac.become_accessory()
    except Exception:
        pass


def send_to_running(payload: dict) -> bool:
    s = QLocalSocket()
    s.connectToServer(IPC_NAME)
    if not s.waitForConnected(300):
        return False
    s.write(json.dumps(payload).encode("utf-8"))
    s.flush()
    s.waitForBytesWritten(500)
    s.disconnectFromServer()
    return True


def setup_logging():
    """Errors go to %LOCALAPPDATA%\\DragShift\\log.txt (pythonw / the .exe have no console)."""
    import faulthandler
    if IS_MAC:
        base = Path.home() / "Library" / "Logs" / "DragShift"
    else:
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / ".local" / "share") / "DragShift"
    base.mkdir(parents=True, exist_ok=True)
    path = base / "log.txt"
    if path.exists() and path.stat().st_size > 1_000_000:
        path.replace(base / "log.old.txt")
    f = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"\n==== DragShift {VERSION} started {time.strftime('%Y-%m-%d %H:%M:%S')} ====\n")
    if sys.stderr is None or getattr(sys, "frozen", False) or sys.executable.lower().endswith("pythonw.exe"):
        sys.stdout = sys.stderr = f
    faulthandler.enable(f)

    def hook(t, v, tb):
        f.write("Unhandled: " + "".join(traceback.format_exception(t, v, tb)))
    sys.excepthook = hook
    return path


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    try:
        setup_logging()
    except Exception:
        pass
    if IS_WIN:
        try:  # helper programs (FFmpeg, OpenSCAD) must never block on a Windows error dialog
            import ctypes
            ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
        except Exception:
            pass
        try:  # crisp taskbar/tray icon grouping
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DragShift.App")
        except Exception:
            pass
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    qapp = DragShiftQApplication(argv)
    if IS_MAC:
        mac_setup(qapp)
    qapp.setApplicationName(APP_NAME)
    qapp.setApplicationVersion(VERSION)
    qapp.setQuitOnLastWindowClosed(False)
    qapp.setWindowIcon(make_icon())

    if "--selftest" in argv:
        return selftest()
    files = [os.path.abspath(a) for a in argv[1:] if not a.startswith("-") and os.path.exists(a)]
    # already running? hand over and exit (instant: no second UI start-up)
    if send_to_running({"files": files} if files else {"show": "--quiet" not in argv}):
        return 0
    app = DragShiftApp(qapp)
    qapp.file_opened.connect(lambda f: app.queue_files([f]))
    if qapp.early_files:
        app.queue_files(list(qapp.early_files))
    if files:  # e.g. "Send to → DragShift" or drag onto the .exe
        QTimer.singleShot(120, lambda: app.queue_files(files))
    elif not app.settings.get("welcomed"):
        QTimer.singleShot(300, app.show_welcome)
    elif "--quiet" not in argv:
        QTimer.singleShot(400, lambda: app.toasts.info(
            "DragShift is running", f"Drag any file, then press {app.trigger_label()}."))
    return qapp.exec()


def check_text_renders():
    """Labels must really draw: paint text with the UI fonts and count the inked pixels."""
    from PySide6.QtGui import QImage, QPainter, QColor
    from .util import ui_font, ui_family
    for display in (True, False):
        img = QImage(200, 40, QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        p.setFont(ui_font(16, True, display))
        p.setPen(QColor(255, 255, 255))
        p.drawText(img.rect(), Qt.AlignCenter, "WEBP 43.8 KB")
        p.end()
        inked = sum(1 for y in range(0, 40, 2) for x in range(0, 200, 2) if img.pixelColor(x, y).alpha() > 60)
        print(f"font {ui_family(display)!r}: {inked} inked samples")
        if inked < 30:
            raise RuntimeError(f"text does not render with font {ui_family(display)!r}")


def selftest() -> int:
    """Used by CI on the frozen .exe: exercise every library and build the UI, then exit."""
    import tempfile
    from PIL import Image
    try:
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "t.png"
            Image.new("RGBA", (64, 48), (255, 100, 80, 255)).save(src)
            for t in convert.targets_for("image", ".png"):
                convert.convert(src, "image", t)
            pdf = convert.convert(src, "image", "PDF")[0]
            for t in convert.PDF_TARGETS:
                convert.convert(pdf, "pdf", t)
            (Path(d) / "n.txt").write_text("hello")
            docx = convert.convert(Path(d) / "n.txt", "text", "DOCX")[0]
            convert.convert(docx, "docx", "TXT")
            imaging.add_background(convert.open_image(src), imaging.Background())
            imaging.compress_pdf(pdf)
            imaging.protect_pdf(pdf, "pw")
            from . import liquid
            liquid.render(Image.new("RGB", (80, 80)), liquid.sdf_circle(80, 80, 40, 40, 30))
            menu = RadialMenu(lambda p: {"Convert": [Item("PNG")]}, lambda p: "1 KB")
            menu._load([str(src)])
            n = lambda *a, **k: None
            for w in (tools.CropWindow(str(src), n), tools.AddBgWindow(str(src), n), tools.EditWindow(str(src), n),
                      tools.AnnotateWindow(str(src), n), tools.RedactWindow(str(src), n),
                      tools.MetadataWindow(str(src), n), tools.CompressWindow([str(src)], n),
                      tools.ResizeWindow([str(src)], n), tools.ProtectWindow(str(pdf), n)):
                w.close()
            ToastStack().info("ok", "ok").dismiss()
            print("selftest: media", flush=True)
            # FFmpeg ships with the app: encode a short clip and convert its audio
            ff = media.ffmpeg_exe()
            print(f"ffmpeg: {ff}")
            clip = Path(d) / "clip.mov"
            import subprocess
            subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1",
                            "-f", "lavfi", "-i", "sine=d=1", "-c:v", "mpeg4", "-c:a", "pcm_s16le", str(clip)],
                           check=True, creationflags=media.NO_WINDOW)
            for t in ("MP4", "HEVC", "MP3"):
                media.convert(clip, "video", t)
            wav = media.convert(clip, "video", "WAV")[0]
            media.convert(wav, "audio", "FLAC")
            media.fit_size(clip, 200_000)
            from . import framegrab
            framegrab.save_frame(clip, 0.5, "PNG")
            framegrab.save_frame(clip, 0.5, "JPG")
            assert framegrab.grab_preview(clip, media.probe(clip), 0.5) is not None
            framegrab.make_window([str(clip)], None).close()
            print("selftest: 3d", flush=True)
            # 3D: mesh formats, preview, STEP through OpenCascade, DXF
            import trimesh
            box = Path(d) / "box.stl"
            trimesh.creation.box((20, 10, 5)).export(box)
            for t in model3d.targets_for("model", ".stl"):
                model3d.convert(box, "model", t)
            for n in model3d.MODEL_TOOLS[:1] + model3d.MODEL_TOOLS[2:]:
                model3d.tool(box, n)
            print("selftest: step/dxf", flush=True)
            import cascadio  # noqa: F401  (STEP backend is bundled)
            import ezdxf
            dxf = Path(d) / "p.dxf"
            dd = ezdxf.new()
            dd.modelspace().add_circle((0, 0), 10)
            dd.saveas(dxf)
            for t in model3d.DXF_TARGETS:
                model3d.convert(dxf, "dxf", t)
            print("selftest: openscad", flush=True)
            try:
                scad = Path(d) / "s.scad"
                scad.write_text("cube(5);")
                print(f"openscad: {model3d.openscad_exe()}")
                model3d.convert(scad, "model", "STL")
            except UserError as e:
                if getattr(sys, "frozen", False) and IS_WIN:
                    raise
                print(f"openscad skipped: {e}")
            print("selftest: windows", flush=True)
            # history, bulk, drop icon, updater pieces
            history.HistoryWindow().close()
            bulk.BulkWindow([d], lambda *a, **k: None).close()
            DropIcon().close()
            assert updater.newer("99.0") and not updater.newer("0.1")
            check_text_renders()
        print("SELFTEST OK")
        return 0
    except Exception:
        traceback.print_exc()
        print("SELFTEST FAILED")
        return 1
