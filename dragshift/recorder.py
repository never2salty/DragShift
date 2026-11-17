"""Screen recording to MP4 or GIF with FFmpeg (gdigrab on Windows, AVFoundation on macOS, x11grab on Linux).

The red frame and the control bar are kept out of the recording.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QPainter, QPen, QScreen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QPushButton, QWidget

from . import capture, media
from .util import UserError

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0


# ---------------------------------------------------------------- FFmpeg command lines
def _even(n: int) -> int:
    return max(2, n - n % 2)


def list_mics() -> list[str]:
    """Microphones FFmpeg can record from (names on Windows, 'index: name' on macOS)."""
    try:
        ff = media.ffmpeg_exe()
    except Exception:
        return []
    if IS_WIN:
        args = [ff, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    elif IS_MAC:
        args = [ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""]
    else:
        return ["default"]
    try:
        p = subprocess.run(args, capture_output=True, timeout=20, creationflags=NO_WINDOW)
    except Exception:
        return []
    return parse_devices(p.stderr.decode("utf-8", "replace"))


def parse_devices(text: str) -> list[str]:
    if "AVFoundation" in text:
        out, audio = [], False
        for ln in text.splitlines():
            if "audio devices" in ln:
                audio = True
                continue
            if "video devices" in ln:
                audio = False
                continue
            m = re.search(r"\]\s*\[(\d+)\]\s*(.+)$", ln)
            if audio and m:
                out.append(f"{m.group(1)}: {m.group(2).strip()}")
        return out
    return [m.group(1) for m in re.finditer(r'"([^"]+)"\s*\(audio\)', text)]


def mac_screen_device(text: str, screen_index: int) -> str:
    m = re.search(rf"\[(\d+)\]\s*Capture screen {screen_index}\b", text)
    return m.group(1) if m else str(screen_index + 1)


def build_args(ff: str, phys: QRect, out: Path, fps: int = 30, mic: str = "", cursor: bool = True,
               gif: bool = False, screen_index: int = 0, mac_listing: str = "") -> list[str]:
    w, h = _even(phys.width()), _even(phys.height())
    a = [ff, "-y", "-hide_banner", "-loglevel", "error", "-thread_queue_size", "1024"]
    vf = []
    if IS_WIN:
        a += ["-f", "gdigrab", "-framerate", str(fps), "-draw_mouse", "1" if cursor else "0",
              "-offset_x", str(phys.x()), "-offset_y", str(phys.y()), "-video_size", f"{w}x{h}", "-i", "desktop"]
        if mic and not gif:
            a += ["-thread_queue_size", "1024", "-f", "dshow", "-i", f"audio={mic}"]
    elif IS_MAC:
        dev = mac_screen_device(mac_listing, screen_index)
        aud = mic.split(":", 1)[0] if mic and not gif else "none"
        a += ["-f", "avfoundation", "-capture_cursor", "1" if cursor else "0", "-framerate", str(fps),
              "-pixel_format", "uyvy422", "-i", f"{dev}:{aud}"]
        vf.append(f"crop={w}:{h}:{phys.x()}:{phys.y()}")
    else:
        disp = os.environ.get("DISPLAY", ":0")
        a += ["-f", "x11grab", "-draw_mouse", "1" if cursor else "0", "-framerate", str(fps),
              "-video_size", f"{w}x{h}", "-i", f"{disp}+{phys.x()},{phys.y()}"]
        if mic and not gif:
            a += ["-f", "pulse", "-i", "default"]
    if vf:
        a += ["-vf", ",".join(vf)]
    if gif:
        a += ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "14", "-pix_fmt", "yuv420p", "-an"]
    else:
        a += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p"]
        if mic:
            a += ["-c:a", "aac", "-b:a", "160k"]
    a += ["-movflags", "+faststart", str(out)]
    return a


def gif_args(ff: str, src: Path, out: Path, fps: int = 15, max_w: int = 960) -> list[str]:
    graph = (f"fps={fps},scale='min({max_w},iw)':-1:flags=lanczos,split[a][b];"
             "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle")
    return [ff, "-y", "-hide_banner", "-loglevel", "error", "-i", str(src), "-vf", graph, "-loop", "0", str(out)]


# ---------------------------------------------------------------- recording
class Recording(QObject):
    finished = Signal(object, float)        # path, seconds
    failed = Signal(str)
    started = Signal()

    def __init__(self, screen: QScreen, local: QRect, gif: bool, out: Path, mic: str = "", fps: int = 30,
                 cursor: bool = True):
        super().__init__()
        self.screen, self.local, self.gif, self.out = screen, local, gif, out
        self.mic, self.fps, self.cursor = mic, fps, cursor
        self.proc: subprocess.Popen | None = None
        self.t0 = 0.0
        self.stopped_at = 0.0
        self.tmp = Path(tempfile.gettempdir()) / f"dragshift-rec-{os.getpid()}-{int(time.time())}.mp4"
        self.log = Path(str(self.tmp) + ".log")
        self._watch = QTimer(self, interval=500, timeout=self._check)

    def start(self):
        ff = media.ffmpeg_exe()
        phys = capture.physical_rect(self.screen, self.local)
        listing = ""
        if IS_MAC:
            p = subprocess.run([ff, "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
                               capture_output=True, timeout=20)
            listing = p.stderr.decode("utf-8", "replace")
        idx = QGuiApplication.screens().index(self.screen) if self.screen in QGuiApplication.screens() else 0
        target = self.tmp if self.gif else self.out
        args = build_args(ff, phys, target, self.fps, self.mic, self.cursor, self.gif, idx, listing)
        self._logf = open(self.log, "wb")
        self.proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=self._logf,
                                     creationflags=NO_WINDOW)
        self.t0 = time.monotonic()
        self._watch.start()
        self.started.emit()

    def elapsed(self) -> float:
        return (self.stopped_at or time.monotonic()) - self.t0 if self.t0 else 0.0

    def _tail(self) -> str:
        try:
            return self.log.read_text("utf-8", "replace").strip().splitlines()[-1][-300:]
        except Exception:
            return ""

    def _check(self):
        if self.proc is not None and self.proc.poll() is not None and not self.stopped_at:
            self._watch.stop()
            self._logf.close()
            msg = self._tail()
            hint = " macOS: allow DragShift under Privacy & Security → Screen Recording." if IS_MAC else ""
            self.failed.emit(f"Recording stopped: {msg or 'FFmpeg quit'}.{hint}")

    def stop(self):
        """Finish the file (and make the GIF) in the background."""
        if self.proc is None or self.stopped_at:
            return
        self.stopped_at = time.monotonic()
        self._watch.stop()
        secs = self.elapsed()
        from .bg import run_bg

        def work():
            try:
                self.proc.stdin.write(b"q")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.proc.kill()
            self._logf.close()
            if self.gif:
                if not self.tmp.exists() or self.tmp.stat().st_size == 0:
                    raise UserError(f"Nothing was recorded. {self._tail()}")
                p = subprocess.run(gif_args(media.ffmpeg_exe(), self.tmp, self.out), capture_output=True,
                                   creationflags=NO_WINDOW)
                self.tmp.unlink(missing_ok=True)
                if p.returncode != 0:
                    raise UserError("Couldn't make the GIF: " + p.stderr.decode("utf-8", "replace")[-200:])
            if not self.out.exists() or self.out.stat().st_size == 0:
                raise UserError(f"Nothing was recorded. {self._tail()}")
            self.log.unlink(missing_ok=True)
            return self.out
        run_bg(work, lambda p: self.finished.emit(p, secs), self.failed.emit)

    def cancel(self):
        self.stopped_at = time.monotonic()
        self._watch.stop()
        if self.proc is not None:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        try:
            self._logf.close()
        except Exception:
            pass
        for p in (self.tmp, self.out, self.log):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------- on-screen pieces
def exclude_from_capture(w: QWidget):
    if IS_WIN:
        try:
            import ctypes
            ctypes.windll.user32.SetWindowDisplayAffinity(int(w.winId()), 0x11)   # WDA_EXCLUDEFROMCAPTURE
        except Exception:
            pass
    elif IS_MAC:
        from . import platform_mac
        platform_mac.exclude_from_capture(w)


class Frame(QWidget):
    """Dashed outline just outside the recorded area; clicks go through."""

    def __init__(self, screen: QScreen, local: QRect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool |
                         Qt.WindowTransparentForInput | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        g = local.translated(screen.geometry().topLeft()).adjusted(-4, -4, 4, 4)
        self.setGeometry(g)
        self.live = False
        self.blink = True

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        col = QColor(255, 59, 48) if self.live else QColor(255, 255, 255, 220)
        p.setPen(QPen(col, 2, Qt.SolidLine if self.live else Qt.DashLine))
        p.drawRect(QRectF(self.rect()).adjusted(1, 1, -1, -1))


BAR_QSS = ("QWidget#bar { background: rgba(26,22,36,0.94); border: 1px solid rgba(255,255,255,0.28); border-radius: 16px; }"
           "QPushButton { background: rgba(255,255,255,0.10); border: 1px solid rgba(255,255,255,0.25); color: white;"
           " border-radius: 12px; padding: 7px 14px; font-weight: 600; }"
           "QPushButton:hover { background: rgba(255,255,255,0.22); }"
           "QPushButton#rec { background: #ff3b30; border: 1px solid rgba(255,255,255,0.5); }"
           "QPushButton#rec:hover { background: #ff5a50; }"
           "QPushButton:checked { background: rgba(255,120,140,0.55); }"
           "QLabel { color: white; font-weight: 600; }")


class Bar(QWidget):
    """Floating control bar next to the recorded area."""

    def __init__(self, screen: QScreen, local: QRect):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.screen_, self.local = screen, local
        inner = QWidget(self)
        inner.setObjectName("bar")
        inner.setStyleSheet(BAR_QSS)
        self.row = QHBoxLayout(inner)
        self.row.setContentsMargins(10, 8, 10, 8)
        self.row.setSpacing(8)
        outer = QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(inner)
        self._drag: QPoint | None = None

    def place(self):
        self.adjustSize()
        g = self.local.translated(self.screen_.geometry().topLeft())
        scr = self.screen_.availableGeometry()
        x = g.center().x() - self.width() // 2
        y = g.bottom() + 14
        if y + self.height() > scr.bottom():
            y = g.top() - self.height() - 14
        if y < scr.top():
            y = g.bottom() - self.height() - 14          # full-screen area: inside, bottom centre
        self.move(max(scr.left() + 8, min(x, scr.right() - self.width() - 8)), y)

    def mousePressEvent(self, e):
        self._drag = e.globalPosition().toPoint() - self.pos()

    def mouseMoveEvent(self, e):
        if self._drag is not None and e.buttons() & Qt.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag)

    def mouseReleaseEvent(self, e):
        self._drag = None


class RecorderUI(QObject):
    """Area picked → setup bar (mic, Record video / GIF) → recording bar (time, Stop) → finished(path, secs)."""
    finished = Signal(object, float, bool)
    failed = Signal(str)
    closed = Signal()

    def __init__(self, screen: QScreen, local: QRect, settings=None):
        super().__init__()
        self.screen, self.local, self.settings = screen, local, settings
        self.rec: Recording | None = None
        self.gif = False
        self.mic = self._get("rec_mic", "")
        self.frame = Frame(screen, local)
        exclude_from_capture(self.frame)
        self.frame.show()
        self.bar = Bar(screen, local)
        self._setup_controls()
        self.bar.show()
        exclude_from_capture(self.bar)
        self.bar.place()
        self.bar.activateWindow()
        self.tick = QTimer(self, interval=250, timeout=self._tick)

    def _get(self, key, default):
        try:
            return self.settings.get(key) if self.settings else default
        except Exception:
            return default

    @property
    def recording(self) -> bool:
        return self.rec is not None and not self.rec.stopped_at

    def _clear(self):
        while self.bar.row.count():
            it = self.bar.row.takeAt(0)
            if it.widget():
                it.widget().deleteLater()

    def _setup_controls(self):
        self._clear()
        r = self.bar.row
        self.mic_btn = QPushButton()
        self.mic_btn.setToolTip("Microphone for MP4 recordings (click to choose)")
        self.mic_btn.clicked.connect(self._mic_menu)
        self._mic_label()
        r.addWidget(self.mic_btn)
        dpr = self.screen.devicePixelRatio()
        size = QLabel(f"{_even(round(self.local.width() * dpr))} × {_even(round(self.local.height() * dpr))}")
        r.addWidget(size)
        full = QPushButton("⤢")
        full.setToolTip("Whole screen")
        full.clicked.connect(self._full)
        r.addWidget(full)
        vid = QPushButton("●  Record video")
        vid.setObjectName("rec")
        vid.clicked.connect(lambda: self.start(False))
        r.addWidget(vid)
        gif = QPushButton("GIF  Record GIF")
        gif.clicked.connect(lambda: self.start(True))
        r.addWidget(gif)
        x = QPushButton("✕")
        x.setToolTip("Cancel (Esc)")
        x.clicked.connect(self.close)
        r.addWidget(x)

    def _mic_label(self):
        name = self.mic.split(": ", 1)[-1] if self.mic else ""
        self.mic_btn.setText(f"🎙 {name[:18]}" if name else "🎙 off")

    def _mic_menu(self):
        m = QMenu(self.bar)
        m.addAction("No microphone", lambda: self._set_mic(""))
        mics = list_mics()
        if not mics:
            a = m.addAction("No microphone found")
            a.setEnabled(False)
        for d in mics:
            act = m.addAction(d.split(": ", 1)[-1], lambda d=d: self._set_mic(d))
            act.setCheckable(True)
            act.setChecked(d == self.mic)
        m.exec(self.mic_btn.mapToGlobal(self.mic_btn.rect().bottomLeft()))

    def _set_mic(self, d):
        self.mic = d
        if self.settings:
            try:
                self.settings.set("rec_mic", d)
            except Exception:
                pass
        self._mic_label()
        self.bar.place()

    def _full(self):
        self.local = QRect(QPoint(0, 0), self.screen.geometry().size())
        self.frame.setGeometry(self.screen.geometry().adjusted(-4, -4, 4, 4))
        self.bar.local = self.local
        self._setup_controls()
        self.bar.place()

    def start(self, gif: bool):
        self.gif = gif
        d = capture.capture_dir(self._get("capture_dir", ""), video=True)
        stem = capture.format_name(self._get("capture_name", capture.DEFAULT_NAME))
        out = capture.unique(d, stem, ".gif" if gif else ".mp4")
        self.rec = Recording(self.screen, self.local, gif, out, "" if gif else self.mic,
                             int(self._get("rec_fps", 30) or 30), bool(self._get("rec_cursor", True)))
        self.rec.finished.connect(self._done)
        self.rec.failed.connect(self._failed)
        self._clear()
        r = self.bar.row
        self.dot = QLabel("●")
        self.dot.setStyleSheet("color: #ff3b30; font-size: 14pt;")
        r.addWidget(self.dot)
        self.time = QLabel("0:00")
        self.time.setMinimumWidth(44)
        r.addWidget(self.time)
        stop = QPushButton("■  Stop")
        stop.setObjectName("rec")
        stop.clicked.connect(self.stop)
        r.addWidget(stop)
        x = QPushButton("✕")
        x.setToolTip("Discard")
        x.clicked.connect(self.close)
        r.addWidget(x)
        self.bar.place()
        self.frame.live = True
        self.frame.update()
        if not (IS_WIN or IS_MAC):
            self.frame.hide()          # X11 without a compositor can't show a see-through window
        try:
            self.rec.start()
        except Exception as e:  # noqa: BLE001
            self._failed(f"Couldn't start recording ({e}).")
            return
        self.tick.start()

    def _tick(self):
        if not self.rec:
            return
        t = self.rec.elapsed()
        s = int(t)
        self.time.setText(f"{s // 60}:{s % 60:02d}")
        self.dot.setStyleSheet(f"color: {'#ff3b30' if int(t * 2) % 2 == 0 else 'rgba(255,59,48,0.25)'}; font-size: 14pt;")

    def stop(self):
        if not self.recording:
            return
        self.tick.stop()
        self.rec.stop()
        self._clear()
        lab = QLabel("Making the GIF…" if self.gif else "Saving…")
        self.bar.row.addWidget(lab)
        self.bar.place()
        self.frame.hide()

    def _done(self, path, secs):
        self.finished.emit(path, secs, self.gif)
        self._teardown()

    def _failed(self, msg):
        self.failed.emit(msg)
        if self.rec:
            self.rec.cancel()
        self._teardown()

    def close(self):
        if self.rec is not None and not self.rec.stopped_at:
            self.rec.cancel()
        self._teardown()

    def _teardown(self):
        self.tick.stop()
        for w in (self.frame, self.bar):
            try:
                w.close()
            except RuntimeError:
                pass
        self.closed.emit()
