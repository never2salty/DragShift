"""Screenshots from a video: a small player with a scrub bar, frame-by-frame steps,
fast playback (1×–16×) and a "Screenshot" button that saves the exact frame in full resolution.

Frames are decoded with FFmpeg (not the system's media player), so everything DragShift can
convert also plays here: ProRes, H.265, MXF, AVI, 10-bit and HDR footage (tone-mapped to SDR).
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
                               QWidget)

from . import media
from .util import CONFIG, UserError, sibling

PREVIEW_MAX = (1280, 720)
PLAY_FPS = 30                  # frames shown per second while playing
SPEEDS = [1, 2, 4, 8, 16]
_HDR = re.compile(r"smpte2084|arib-std-b67|bt2020")
# Preview/playback decode on the GPU on Windows (D3D11/DXVA); switched off after the first failure
_HWACCEL = {"on": media.IS_WIN}


def _hw() -> list[str]:
    return ["-hwaccel", "auto"] if _HWACCEL["on"] else []


# ---------------------------------------------------------------- FFmpeg helpers
_FILTERS: set[str] | None = None


def _has_filter(name: str) -> bool:
    global _FILTERS
    if _FILTERS is None:
        try:
            out = media._run_quick(["-filters"], timeout=20).stdout.decode("utf-8", "replace")
            _FILTERS = {line.split()[1] for line in out.splitlines() if len(line.split()) > 2}
        except Exception:
            _FILTERS = set()
    return name in _FILTERS


def is_hdr(info: media.Info) -> bool:
    for line in info.raw.splitlines():
        if ": Video: " in line and "(attached pic)" not in line:
            return bool(_HDR.search(line)) and info.ten_bit
    return False


def frame_filter(info: media.Info, preview: bool) -> str:
    """Square pixels, SDR colours (HDR tone-mapped), preview scaled down."""
    f = ["scale=iw*sar:ih", "setsar=1"]
    if preview:  # shrink first: tone-mapping a 4K frame in float is the slow part
        w, h = PREVIEW_MAX
        f.append(f"scale='min({w},iw)':'min({h},ih)':force_original_aspect_ratio=decrease:flags=bilinear")
    if is_hdr(info) and _has_filter("zscale") and _has_filter("tonemap"):
        f += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
              "tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    f.append("format=rgb24")
    return ",".join(f)


def frame_time(n: int, fps: float) -> float:
    """Seek target that lands exactly on frame n (FFmpeg returns the first frame with pts ≥ target)."""
    return max(0.0, (n - 0.25) / fps)


def _read_bmp(stream) -> bytes | None:
    """One BMP from an image2pipe stream (its size is in the header)."""
    head = _read_exact(stream, 14)
    if head is None or head[:2] != b"BM":
        return None
    size = int.from_bytes(head[2:6], "little")
    rest = _read_exact(stream, size - 14)
    return None if rest is None else head + rest


def _read_exact(stream, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


def grab_preview(src: Path, info: media.Info, t: float, fast: bool = False) -> QImage | None:
    """One frame at time t, scaled for the screen. fast=True: nearest keyframe (for scrubbing)."""
    for attempt in range(2):
        args = _hw() + (["-noaccurate_seek"] if fast else []) + [
            "-ss", f"{t:.4f}", "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-an", "-sn",
            "-vf", frame_filter(info, True), "-f", "image2pipe", "-c:v", "bmp", "-"]
        try:
            p = media._run_quick(["-loglevel", "error", *args], timeout=60)
        except subprocess.TimeoutExpired:
            return None
        img = QImage.fromData(p.stdout, "BMP") if p.returncode == 0 and p.stdout else QImage()
        if not img.isNull():
            return img
        if not _HWACCEL["on"]:
            return None
        _HWACCEL["on"] = False      # GPU decode failed on this machine: software from now on
    return None


def save_frame(src: str | Path, t: float, fmt: str = "PNG", info: media.Info | None = None,
               shown_at: float | None = None) -> Path:
    """Full-resolution frame at seek time t → 'Clip 01m23.456s.png' next to the video.
    shown_at: the frame's own time for the file name (defaults to t)."""
    src = Path(src)
    info = info or media.probe(src)
    fmt = fmt.upper()
    m, s = divmod(max(0.0, t if shown_at is None else shown_at), 60)
    out = sibling(src, f"{int(m):02d}m{s:06.3f}s", ".jpg" if fmt == "JPG" else ".png")
    codec = ["-c:v", "mjpeg", "-q:v", "2", "-pix_fmt", "yuvj444p"] if fmt == "JPG" else ["-c:v", "png"]
    vf = frame_filter(info, False)
    if fmt == "JPG":
        vf = vf.replace("format=rgb24", "format=yuvj444p")
    res = media.run_ffmpeg(["-ss", f"{t:.4f}", "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-an", "-sn",
                            "-vf", vf, *codec, "-update", "1"], out, None)
    if res is None:
        raise UserError("Couldn't save the frame.")
    return res


# ---------------------------------------------------------------- background decoder
class FrameSource(QObject):
    """Decodes frames off the UI thread. Only the newest seek request is served."""
    frame = Signal(QImage, float, bool)   # image, its time, exact (False while scrubbing fast)
    ended = Signal()

    def __init__(self, src: Path, info: media.Info):
        super().__init__()
        self.src, self.info = src, info
        self._cv = threading.Condition()
        self._req: tuple[float, bool] | None = None
        self._play: tuple[float, float] | None = None   # (start, speed)
        self._stop = False
        self._proc: subprocess.Popen | None = None
        self._gen = 0
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def seek(self, t: float, fast: bool = False):
        with self._cv:
            self._gen += 1
            self._play = None
            self._req = (t, fast)
            self._kill()
            self._cv.notify()

    def play(self, t: float, speed: float):
        with self._cv:
            self._gen += 1
            self._req = None
            self._play = (t, speed)
            self._kill()
            self._cv.notify()

    def pause(self):
        with self._cv:
            self._gen += 1
            self._play = None
            self._kill()

    def close(self):
        with self._cv:
            self._stop = True
            self._gen += 1
            self._kill()
            self._cv.notify()

    def _kill(self):
        if self._proc is not None and self._proc.poll() is None:
            try:
                self._proc.kill()
            except OSError:
                pass

    def _loop(self):
        while True:
            with self._cv:
                while not self._stop and self._req is None and self._play is None:
                    self._cv.wait()
                if self._stop:
                    return
                gen = self._gen
                req, play = self._req, self._play
                self._req = None
            if req is not None:
                t, fast = req
                img = grab_preview(self.src, self.info, t, fast)
                if img is not None and gen == self._gen:
                    self.frame.emit(img, t, not fast)
            elif play is not None:
                self._stream(gen, *play)

    def _stream(self, gen: int, start: float, speed: float):
        """Play from `start`: FFmpeg speeds the video up and outputs PLAY_FPS frames per second."""
        vf = f"setpts=PTS/{speed},fps={PLAY_FPS}," + frame_filter(self.info, True)
        args = [media.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", *_hw(), "-ss", f"{start:.4f}",
                "-i", str(self.src), "-map", "0:v:0", "-an", "-sn", "-vf", vf, "-f", "image2pipe", "-c:v", "bmp", "-"]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    stdin=subprocess.DEVNULL, creationflags=media.NO_WINDOW)
        except OSError:
            return
        with self._cv:
            if gen != self._gen:
                proc.kill()
                return
            self._proc = proc
        t0 = time.monotonic()
        k = 0
        try:
            while gen == self._gen:
                data = _read_bmp(proc.stdout)
                if data is None:
                    if gen == self._gen:
                        with self._cv:
                            self._play = None
                        self.ended.emit()
                    return
                img = QImage.fromData(data, "BMP")
                due = t0 + k / PLAY_FPS
                wait = due - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                if gen != self._gen:
                    return
                self.frame.emit(img, start + k * speed / PLAY_FPS, False)
                k += 1
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


# ---------------------------------------------------------------- widgets
class FrameView(QWidget):
    def __init__(self):
        super().__init__()
        self.img: QImage | None = None
        self.flash = 0.0
        self.message = "Loading…"
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._ft = QTimer(self, interval=16, timeout=self._fade)

    def set_image(self, img: QImage):
        self.img = img
        self.message = ""
        self.update()

    def blink(self):
        self.flash = 1.0
        self._ft.start()

    def _fade(self):
        self.flash = max(0.0, self.flash - 0.08)
        if self.flash == 0:
            self._ft.stop()
        self.update()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        p.drawRoundedRect(r, 12, 12)
        if self.img is not None and not self.img.isNull():
            iw, ih = self.img.width(), self.img.height()
            k = min(r.width() / iw, r.height() / ih)
            w, h = iw * k, ih * k
            target = QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h)
            p.drawImage(target, self.img)
            if self.flash > 0:
                p.fillRect(target, QColor(255, 255, 255, int(200 * self.flash)))
        if self.message:
            p.setPen(QColor(255, 255, 255, 180))
            p.drawText(r, Qt.AlignCenter, self.message)


def fmt_time(t: float, fps: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((t - int(t)) * 1000)) % 1000
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}"


def make_window(paths, notify):
    from .window import GlassWindow

    class _Win(GlassWindow):
        def __init__(self):
            super().__init__("Screenshots from video", size=(980, 760))
            self.src = Path(paths[0])
            self.notify = notify
            self.info = media.probe(self.src)
            if not self.info.has_video:
                raise UserError("No video track found in this file.")
            self.fps = self.info.fps if self.info.fps and self.info.fps < 1000 else 25.0
            self.dur = self.info.duration or 0.0
            self.frames = max(1, int(round(self.dur * self.fps)))
            self.n = 0                  # current frame index
            self.t = 0.0                # time of the frame on screen
            self.playing = False
            self.saved: list[Path] = []
            w, h = self.info.display_size

            sub = QLabel(f"{self.src.name}  ·  {w}×{h}  ·  {self.fps:g} fps  ·  {media.fmt_duration(self.dur)}"
                         + ("  ·  HDR → SDR" if is_hdr(self.info) else ""))
            sub.setObjectName("dim")
            self.body.addWidget(sub)

            self.view = FrameView()
            self.body.addWidget(self.view, 1)

            # scrub bar + time
            row = QHBoxLayout()
            self.slider = QSlider(Qt.Horizontal)
            self.slider.setRange(0, self.frames - 1)
            self.slider.setPageStep(max(1, int(self.fps)))
            self.slider.setFocusPolicy(Qt.NoFocus)  # arrow keys are frame steps, handled below
            self.slider.valueChanged.connect(self._slid)
            self.slider.sliderReleased.connect(lambda: self.goto(self.slider.value(), exact=True))
            row.addWidget(self.slider, 1)
            self.clock = QLabel()
            self.clock.setMinimumWidth(230)
            self.clock.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.clock.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace;")
            row.addWidget(self.clock)
            self.body.addLayout(row)

            # transport
            bar = QHBoxLayout()
            bar.setSpacing(6)

            def btn(text, tip, fn, key=None):
                b = QPushButton(text)
                b.setToolTip(tip + (f"  ({key})" if key else ""))
                b.setFocusPolicy(Qt.NoFocus)
                b.clicked.connect(fn)
                bar.addWidget(b)
                return b

            sec = max(1, int(round(self.fps)))
            btn("−10 s", "Back 10 seconds", lambda: self.step(-10 * sec), "Shift+↑")
            btn("−1 s", "Back 1 second", lambda: self.step(-sec), "Shift+←")
            btn("◀ 1", "Previous frame", lambda: self.step(-1), "←")
            self.play_btn = btn("▶  Play", "Play / pause", self.toggle_play, "Space")
            self.play_btn.setMinimumWidth(96)
            btn("1 ▶", "Next frame", lambda: self.step(1), "→")
            btn("+1 s", "Forward 1 second", lambda: self.step(sec), "Shift+→")
            btn("+10 s", "Forward 10 seconds", lambda: self.step(10 * sec), "Shift+↓")
            self.speed = QComboBox()
            self.speed.setFocusPolicy(Qt.NoFocus)
            for s in SPEEDS:
                self.speed.addItem(f"{s}×", s)
            self.speed.setToolTip("Playback speed  (1–5)")
            self.speed.currentIndexChanged.connect(self._speed_changed)
            bar.addWidget(self.speed)
            bar.addStretch(1)
            self.fmt = QComboBox()
            self.fmt.setFocusPolicy(Qt.NoFocus)
            self.fmt.addItem("PNG · lossless", "PNG")
            self.fmt.addItem("JPG · smaller", "JPG")
            self.fmt.setCurrentIndex(1 if CONFIG.get("frame_format") == "JPG" else 0)
            self.fmt.currentIndexChanged.connect(lambda _: CONFIG.__setitem__("frame_format", self.fmt.currentData()))
            bar.addWidget(self.fmt)
            self.shot_btn = QPushButton("📷  Screenshot")
            self.shot_btn.setObjectName("primary")
            self.shot_btn.setToolTip("Save this frame in full resolution  (S or Enter)")
            self.shot_btn.setFocusPolicy(Qt.NoFocus)
            self.shot_btn.clicked.connect(self.screenshot)
            bar.addWidget(self.shot_btn)
            self.body.addLayout(bar)

            foot = QHBoxLayout()
            self.status = QLabel("← → frame by frame · Shift for 1 s · Space plays · S takes a screenshot")
            self.status.setObjectName("dim")
            foot.addWidget(self.status, 1)
            self.open_btn = QPushButton("Show in folder")
            self.open_btn.setEnabled(False)
            self.open_btn.clicked.connect(self._reveal)
            foot.addWidget(self.open_btn)
            done = QPushButton("Done")
            done.clicked.connect(self.close)
            foot.addWidget(done)
            self.body.addLayout(foot)

            keys = [(Qt.Key_Left, lambda: self.step(-1)), (Qt.Key_Right, lambda: self.step(1)),
                    ("Shift+Left", lambda: self.step(-sec)), ("Shift+Right", lambda: self.step(sec)),
                    ("Shift+Up", lambda: self.step(-10 * sec)), ("Shift+Down", lambda: self.step(10 * sec)),
                    (Qt.Key_Comma, lambda: self.step(-1)), (Qt.Key_Period, lambda: self.step(1)),
                    (Qt.Key_Space, self.toggle_play), (Qt.Key_K, self.toggle_play),
                    (Qt.Key_S, self.screenshot), (Qt.Key_Return, self.screenshot), (Qt.Key_Enter, self.screenshot),
                    (Qt.Key_Home, lambda: self.goto(0)), (Qt.Key_End, lambda: self.goto(self.frames - 1))]
            keys += [(getattr(Qt, f"Key_{i + 1}"), (lambda i=i: self.speed.setCurrentIndex(i))) for i in range(len(SPEEDS))]
            for k, fn in keys:
                QShortcut(QKeySequence(k), self, activated=fn)

            _has_filter("zscale")      # cache FFmpeg's filter list before the first frame
            self.source = FrameSource(self.src, self.info)
            self.source.frame.connect(self._on_frame)
            self.source.ended.connect(self._on_end)
            self._slide_guard = False
            self._update_clock()
            self.source.seek(0.0)

        # ------------------------------------------------ navigation
        def goto(self, n: int, exact: bool = True):
            self.pause()
            self.n = max(0, min(self.frames - 1, int(n)))
            self.t = self.n / self.fps
            self._set_slider(self.n)
            self._update_clock()
            self.source.seek(frame_time(self.n, self.fps), fast=not exact)

        def step(self, k: int):
            self.goto(self.n + k)

        def _slid(self, v: int):
            if self._slide_guard:
                return
            # while dragging show the nearest keyframe instantly; the exact frame comes on release
            self.goto(v, exact=not self.slider.isSliderDown())

        def _set_slider(self, n: int):
            self._slide_guard = True
            self.slider.setValue(n)
            self._slide_guard = False

        # ------------------------------------------------ playback
        def toggle_play(self):
            if self.playing:
                self.pause()
                self.goto(self.n)          # settle on an exact frame
            else:
                if self.n >= self.frames - 1:
                    self.n = 0
                self.playing = True
                self.play_btn.setText("❚❚  Pause")
                self.source.play(self.n / self.fps, self.speed.currentData())

        def pause(self):
            if self.playing:
                self.playing = False
                self.play_btn.setText("▶  Play")
                self.source.pause()

        def _speed_changed(self, *_):
            if self.playing:
                self.source.play(self.n / self.fps, self.speed.currentData())

        def _on_frame(self, img: QImage, t: float, exact: bool):
            self.view.set_image(img)
            if self.playing:
                self.t = t
                self.n = max(0, min(self.frames - 1, int(round(t * self.fps))))
                self._set_slider(self.n)
                self._update_clock()

        def _on_end(self):
            self.pause()
            self.goto(self.frames - 1)

        def _update_clock(self):
            self.clock.setText(f"{fmt_time(self.n / self.fps, self.fps)}  ·  frame {self.n + 1} / {self.frames}")

        # ------------------------------------------------ screenshot
        def screenshot(self):
            was_playing = self.playing
            if was_playing:
                self.pause()
            n, fmt = self.n, self.fmt.currentData()
            t = frame_time(n, self.fps)
            self.view.blink()
            self.shot_btn.setEnabled(False)
            self.status.setText(f"Saving frame {n + 1}…")
            holder: dict = {}

            def work():
                try:
                    holder["out"] = save_frame(self.src, t, fmt, self.info, shown_at=n / self.fps)
                except Exception as e:  # noqa: BLE001
                    holder["err"] = str(e)

            th = threading.Thread(target=work, daemon=True)
            th.start()

            def check():
                if th.is_alive():
                    QTimer.singleShot(40, check)
                    return
                self.shot_btn.setEnabled(True)
                if "err" in holder:
                    self.status.setText(f"Couldn't save: {holder['err']}")
                    return
                out = holder["out"]
                self.saved.append(out)
                self.open_btn.setEnabled(True)
                n_s = len(self.saved)
                self.status.setText(f"✓ Saved {out.name}" + (f"  ·  {n_s} screenshots so far" if n_s > 1 else ""))
                if was_playing:
                    self.goto(n)
            QTimer.singleShot(40, check)

        def _reveal(self):
            from .toasts import reveal
            if self.saved:
                reveal(self.saved[-1])

        def closeEvent(self, e):
            self.source.close()
            if self.saved and self.notify:
                try:
                    self.notify("tool:Screenshots", [self.src], list(self.saved), "")
                except Exception:
                    pass
                self.saved = []
            super().closeEvent(e)

    return _Win()
