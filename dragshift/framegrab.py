"""Video player for screenshots, colour grading and graded exports.

* Picture: FFmpeg streams raw frames (no per-frame image files), sized for the screen, with the
  grade applied as one 3D LUT. Sound: decoded by FFmpeg, played with sounddevice, and the video
  follows the audio clock, so playback is smooth and in sync.
* Stills (paused, stepping, scrubbing) are decoded once and graded instantly in numpy, so the
  colour sliders react while you drag them.
* Screenshot: the exact frame at full resolution, graded in 16-bit, as PNG or JPG.
* Export: the whole clip with the grade baked in (H.264, H.265 10-bit or ProRes 422 HQ).
"""
from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel,
                               QPushButton, QScrollArea, QSizePolicy, QSlider, QVBoxLayout, QWidget)

from . import grade as G
from . import media
from .audioplay import AudioPlayer
from .util import CONFIG, UserError, sibling

PREVIEW_MAX = (1280, 720)
SPEEDS = [1, 2, 4, 8, 16]
_HDR = re.compile(r"smpte2084|arib-std-b67")
_HWACCEL = {"on": media.IS_WIN}   # GPU decode for preview/playback on Windows; off after a failure


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


def _video_line(info: media.Info) -> str:
    for line in info.raw.splitlines():
        if ": Video: " in line and "(attached pic)" not in line:
            return line
    return ""


def is_hdr(info: media.Info) -> bool:
    return bool(_HDR.search(_video_line(info))) and info.ten_bit


def sar(info: media.Info) -> float:
    m = re.search(r"SAR (\d+):(\d+)", _video_line(info))
    if m and int(m[2]):
        return int(m[1]) / int(m[2]) or 1.0
    return 1.0


def preview_size(info: media.Info) -> tuple[int, int]:
    w, h = info.display_size
    s = sar(info)
    if abs(info.rotation) % 180 == 90:
        h = h * s
    else:
        w = w * s
    w, h = max(2, w), max(2, h)
    k = min(1.0, PREVIEW_MAX[0] / w, PREVIEW_MAX[1] / h)
    return max(2, int(w * k) // 2 * 2), max(2, int(h * k) // 2 * 2)


def _in_matrix(info: media.Info) -> str:
    """Untagged HD video is BT.709; FFmpeg would otherwise assume BT.601 and shift colours."""
    line = _video_line(info)
    if re.search(r"bt709|bt2020|smpte170m|bt470bg|smpte240m", line) or info.height < 600:
        return ""
    return ":in_color_matrix=bt709"


def _zscale_in(info: media.Info) -> str:
    """zimg input description from the stream tags (BT.709 / limited when untagged)."""
    line = _video_line(info)
    if re.search(r"bt2020", line):
        m = "2020_ncl"
    elif re.search(r"smpte170m|bt470bg", line) or (info.height < 600 and "bt709" not in line):
        m = "170m"
    else:
        m = "709"
    r = "full" if re.search(r"\((pc|jpeg)[,)]", line) or "yuvj" in line else "limited"
    return f"min={m}:rin={r}"


def frame_filter(info: media.Info, preview: bool, grade: G.Grade | None = None, export: bool = False) -> str:
    """Square pixels, preview size, HDR → SDR when there's no grade, the grade as a 3D LUT.
    YUV → RGB goes through zimg (zscale): FFmpeg 9's swscale tints neutral greys slightly, and log
    footage + LUT turns that into a visible colour cast."""
    graded = grade is not None and not grade.is_identity()
    hdr = not graded and is_hdr(info) and _has_filter("zscale") and _has_filter("tonemap")
    use_z = _has_filter("zscale") and not hdr
    if preview:
        pw, ph = preview_size(info)
        if use_z:
            f = [f"zscale=w={pw}:h={ph}:f=bilinear:{_zscale_in(info)}", "setsar=1"]
        else:
            f = [f"scale={pw}:{ph}:flags=bilinear{_in_matrix(info)}", "setsar=1"]
    else:
        if use_z:
            f = [f"zscale=w=trunc(iw*sar/2)*2:h=ih:f=lanczos:{_zscale_in(info)}", "setsar=1"]
        else:
            f = [f"scale=iw*sar:ih:flags=lanczos{_in_matrix(info)}", "setsar=1"]
    if use_z:
        f.append("format=gbrpf32le" if graded else "format=gbrp")
    if hdr:
        f += ["zscale=t=linear:npl=100", "format=gbrpf32le", "zscale=p=bt709",
              "tonemap=hable:desat=0", "zscale=t=bt709:m=bt709:r=tv"]
    if graded:
        if preview:
            f += ([] if use_z else ["format=rgb24"]) + [G.lut3d_filter(grade, 33, "trilinear")[0]]
        else:
            f += ([] if use_z else ["format=rgb48le"]) + [G.lut3d_filter(grade, 65, "tetrahedral")[0]]
    if not export:
        f.append("format=rgb24")
    return ",".join(f)


def frame_time(n: int, fps: float) -> float:
    """Seek target that lands exactly on frame n (FFmpeg returns the first frame with pts ≥ target)."""
    return max(0.0, (n - 0.25) / fps)


def grab_raw(src: Path, info: media.Info, t: float, fast: bool = False, hdr_map: bool = True) -> np.ndarray | None:
    """One ungraded frame at time t at preview size, as uint8 (H, W, 3). fast: nearest keyframe."""
    pw, ph = preview_size(info)
    vf = frame_filter(info, True) if hdr_map else (
        f"zscale=w={pw}:h={ph}:f=bilinear:{_zscale_in(info)},setsar=1,format=gbrp,format=rgb24" if _has_filter("zscale")
        else f"scale={pw}:{ph}:flags=bilinear{_in_matrix(info)},setsar=1,format=rgb24")
    for _ in range(2):
        args = _hw() + (["-noaccurate_seek"] if fast else []) + [
            "-ss", f"{t:.4f}", "-i", str(Path(src).resolve()), "-map", "0:v:0", "-frames:v", "1", "-an", "-sn",
            "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        try:
            p = media._run_quick(["-loglevel", "error", *args], timeout=60)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode == 0 and len(p.stdout) >= pw * ph * 3:
            return np.frombuffer(p.stdout[: pw * ph * 3], dtype=np.uint8).reshape(ph, pw, 3)
        if not _HWACCEL["on"]:
            return None
        _HWACCEL["on"] = False
    return None


def to_qimage(a: np.ndarray) -> QImage:
    a = np.ascontiguousarray(a)
    h, w = a.shape[:2]
    return QImage(a.data, w, h, 3 * w, QImage.Format_RGB888).copy()


def grab_preview(src: Path, info: media.Info, t: float, fast: bool = False) -> QImage | None:
    a = grab_raw(src, info, t, fast)
    return None if a is None else to_qimage(a)


def save_frame(src: str | Path, t: float, fmt: str = "PNG", info: media.Info | None = None,
               shown_at: float | None = None, grade: G.Grade | None = None) -> Path:
    """Full-resolution frame at seek time t → 'Clip 01m23.456s.png' next to the video."""
    src = Path(src).resolve()
    info = info or media.probe(src)
    fmt = fmt.upper()
    m, s = divmod(max(0.0, t if shown_at is None else shown_at), 60)
    out = sibling(src, f"{int(m):02d}m{s:06.3f}s", ".jpg" if fmt == "JPG" else ".png").resolve()
    vf = frame_filter(info, False, grade)
    if fmt == "JPG":
        vf = vf[: -len("format=rgb24")] + "scale=out_color_matrix=bt601:out_range=pc,format=yuvj444p"  # JFIF = BT.601
        codec = ["-c:v", "mjpeg", "-q:v", "2"]
    else:
        codec = ["-c:v", "png"]
    res = media.run_ffmpeg(["-ss", f"{t:.4f}", "-i", str(src), "-map", "0:v:0", "-frames:v", "1", "-an", "-sn",
                            "-vf", vf, *codec, "-update", "1"], out, None, cwd=G.cube_dir())
    if res is None:
        raise UserError("Couldn't save the frame.")
    return res


EXPORTS = {"H264": ("H.264 MP4", ".mp4"), "HEVC": ("H.265 MP4 · 10-bit", ".mp4"), "PRORES": ("ProRes 422 HQ", ".mov")}


def _span(t0: float | None, t1: float | None, info: media.Info) -> tuple[list[str], list[str], float | None]:
    """(-ss before -i, -t after -i, duration) for a range; exact because FFmpeg decodes from the keyframe."""
    if t0 is None and t1 is None:
        return [], [], info.duration
    a = max(0.0, t0 or 0.0)
    b = t1 if t1 is not None else (info.duration or a)
    dur = max(0.04, b - a)
    return ["-ss", f"{a:.4f}"], ["-t", f"{dur:.4f}"], dur


def export_graded(src: str | Path, g: G.Grade, target: str = "H264", t0: float | None = None,
                  t1: float | None = None, label: str | None = None) -> list[Path]:
    """The clip (or the range t0..t1) with the grade baked in, Rec.709-tagged."""
    src = Path(src).resolve()
    info = media.probe(src)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    pre, post, dur = _span(t0, t1, info)
    if label is None:
        label = ("trim " if pre else "") + ("graded" if not g.is_identity() else "")
        label = label.strip() or "export"
    ext = EXPORTS[target][1]
    out = sibling(src, label, ext).resolve()
    vf = frame_filter(info, False, g, export=True)
    tags = ["-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv"]
    maps = ["-map", "0:v:0", "-map", "0:a?"]
    if target == "PRORES":
        pcm = "pcm_s24le" if info.abits > 16 else "pcm_s16le"
        a = ["-c:a", pcm] if info.has_audio else []
        vf += (",format=gbrpf32le,zscale=m=709:r=limited,format=yuv422p10le" if _has_filter("zscale")
               else ",scale=out_color_matrix=bt709:out_range=tv,format=yuv422p10le")
        args = [*pre, "-i", str(src), *post, *maps, "-vf", vf, "-c:v", "prores_ks", "-profile:v", "3",
                "-vendor", "apl0", *a, *tags, *media._common_out_flags(ext)]
        return [media.run_ffmpeg(args, out, dur, cwd=G.cube_dir())]
    codec = "h264" if target == "H264" else "hevc"
    fake = replace(info, pix_fmt="yuv420p10le" if codec == "hevc" else "yuv420p")
    pix = "yuv420p10le" if codec == "hevc" else "yuv420p"
    vf += (f",format=gbrpf32le,zscale=m=709:r=limited,format={pix}" if _has_filter("zscale")
           else f",scale=out_color_matrix=bt709:out_range=tv,format={pix}")

    def args_for(hw):
        v, gpu = media._video_args(codec, fake, media._quality(), hw)
        return [*pre, "-i", str(src), *post, *maps, "-vf", vf, *v, *media._aac_args(info, allow_copy=not pre), *tags,
                *media._common_out_flags(ext)], gpu
    a, gpu = args_for(True)
    try:
        return [media.run_ffmpeg(a, out, dur, cwd=G.cube_dir())]
    except media.FFmpegError:
        if not gpu:
            raise
        a, _ = args_for(False)
        return [media.run_ffmpeg(a, out, dur, cwd=G.cube_dir())]


def trim_fast(src: str | Path, t0: float, t1: float) -> list[Path]:
    """Cut without re-encoding: instant and lossless, but video starts at the keyframe before t0."""
    src = Path(src).resolve()
    info = media.probe(src)
    pre, post, dur = _span(t0, t1, info)
    ext = src.suffix.lower()
    out = sibling(src, "trim", ext).resolve()
    args = [*pre, "-i", str(src), *post, "-map", "0:v?", "-map", "0:a?", "-c", "copy",
            "-avoid_negative_ts", "make_zero", *media._common_out_flags(ext)]
    return [media.run_ffmpeg(args, out, dur)]


def trim_audio(src: str | Path, t0: float, t1: float) -> list[Path]:
    """Sample-exact audio cut, same format as the original (WAV stays WAV, MP3 stays MP3 at 320k…)."""
    src = Path(src).resolve()
    info = media.probe(src)
    if not info.has_audio:
        raise UserError("No audio track found in this file.")
    target = media._same_format_target(src)
    pre, post, dur = _span(t0, t1, info)
    ext = media.TARGET_EXT[target]
    out = sibling(src, "trim", ext).resolve()
    maps = media._audio_maps(target, info, info.has_video)
    args = [*pre, "-i", str(src), *post, *maps, *media._audio_codec(target, info), *media._common_out_flags(ext)]
    if target == "OPUS" and info.sample_rate not in (8000, 12000, 16000, 24000, 48000):
        args += ["-ar", "48000"]
    return [media.run_ffmpeg(args, out, dur)]


def waveform(src: str | Path, w: int = 1600, h: int = 360) -> QImage | None:
    """The audio as a picture (FFmpeg showwavespic), for the audio player and trimming."""
    p = media._run_quick(["-loglevel", "error", "-i", str(Path(src).resolve()), "-filter_complex",
                          f"[0:a:0]aformat=channel_layouts=mono,showwavespic=s={w}x{h}:colors=0x8fb4ff:scale=sqrt[v]",
                          "-map", "[v]", "-frames:v", "1", "-f", "image2pipe", "-c:v", "png", "-"], timeout=600)
    img = QImage.fromData(p.stdout, "PNG") if p.returncode == 0 and p.stdout else QImage()
    return None if img.isNull() else img


def thumbnail_image(src: str | Path, info: media.Info | None = None, w: int = 192) -> QImage | None:
    """Small picture for the clip list: a frame at 10 % for video, the waveform for audio."""
    try:
        info = info or media.probe(src)
    except UserError:
        return None
    if not info.has_video:
        return waveform(src, w, w * 9 // 16)
    t = min((info.duration or 0) * 0.1, 3.0)
    p = media._run_quick(["-loglevel", "error", "-noaccurate_seek", "-ss", f"{t:.2f}", "-i", str(Path(src).resolve()),
                          "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={w}:-2", "-f", "image2pipe",
                          "-c:v", "png", "-"], timeout=60)
    img = QImage.fromData(p.stdout, "PNG") if p.returncode == 0 and p.stdout else QImage()
    return None if img.isNull() else img


# ---------------------------------------------------------------- playback clock
class Clock:
    """Video time now: the audio clock when sound plays, otherwise the wall clock."""

    def __init__(self, start: float, speed: float, audio: AudioPlayer | None):
        self.start, self.speed, self.audio = start, speed, audio
        self.t0 = time.monotonic()

    def now(self) -> float:
        wall = self.start + (time.monotonic() - self.t0) * self.speed
        a = self.audio.position() if self.audio is not None else None
        if a is None:
            return wall
        if a < wall - 0.5 and self.audio._eof:      # audio ran out (shorter track): keep going
            return wall
        return a


# ---------------------------------------------------------------- background decoder
class FrameSource(QObject):
    still = Signal(object, float, bool)     # raw uint8 frame, time, exact
    frame = Signal(QImage, float)           # graded playback frame, time
    ended = Signal()

    def __init__(self, src: Path, info: media.Info):
        super().__init__()
        self.src, self.info = Path(src).resolve(), info
        self.audio = AudioPlayer()
        self._cv = threading.Condition()
        self._req: tuple[float, bool, bool] | None = None
        self._play: tuple[float, float, G.Grade] | None = None
        self._stop = False
        self._proc: subprocess.Popen | None = None
        self._gen = 0
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def seek(self, t: float, fast: bool = False, hdr_map: bool = True):
        with self._cv:
            self._gen += 1
            self._play = None
            self._req = (t, fast, hdr_map)
            self._kill()
            self._cv.notify()
        self.audio.stop()

    def play(self, t: float, speed: float, grade: G.Grade):
        with self._cv:
            self._gen += 1
            self._req = None
            self._play = (t, speed, grade)
            self._kill()
            self._cv.notify()

    def pause(self):
        with self._cv:
            self._gen += 1
            self._play = None
            self._kill()
        self.audio.stop()

    def close(self):
        with self._cv:
            self._stop = True
            self._gen += 1
            self._kill()
            self._cv.notify()
        self.audio.stop()

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
                t, fast, hdr_map = req
                a = grab_raw(self.src, self.info, t, fast, hdr_map)
                if a is not None and gen == self._gen:
                    self.still.emit(a, t, not fast)
            elif play is not None:
                try:
                    self._stream(gen, *play)
                except Exception:   # never let a playback hiccup kill the decoder thread
                    with self._cv:
                        if gen == self._gen:
                            self._play = None
                    self.ended.emit()

    def _stream(self, gen: int, start: float, speed: float, grade: G.Grade):
        info = self.info
        pw, ph = preview_size(info)
        src_fps = info.fps if 0 < info.fps < 1000 else 25.0
        out_fps = min(src_fps * speed, 60.0) if speed > 1 else src_fps
        vf = []
        if speed > 1:
            vf += [f"setpts=PTS/{speed}", f"fps={out_fps:.4f}"]
        vf.append(frame_filter(info, True, grade))
        args = [media.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", *_hw(),
                *(["-skip_frame", "nokey"] if speed >= 8 else []), "-ss", f"{start:.4f}",
                "-i", str(self.src), "-map", "0:v:0", "-an", "-sn", "-vf", ",".join(vf),
                "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                                creationflags=media.NO_WINDOW, cwd=str(G.cube_dir()), bufsize=pw * ph * 3 * 4)
        with self._cv:
            if gen != self._gen:
                proc.kill()
                return
            self._proc = proc
        size = pw * ph * 3
        step = speed / out_fps          # video seconds per output frame
        first = proc.stdout.read(size)  # decoder warm-up happens before the clock starts
        if len(first) < size:
            proc.kill()
            if gen == self._gen:
                with self._cv:
                    self._play = None
                self.ended.emit()
            return
        self.audio.start(self.src, start, speed, info)
        clock = Clock(start, speed, self.audio if self.audio.active else None)
        k = 0
        data = first
        last_emit = 0.0
        try:
            while gen == self._gen:
                vt = start + k * step
                # wait until it's this frame's turn
                while gen == self._gen:
                    ahead = vt - clock.now()
                    if ahead <= 0.002:
                        break
                    time.sleep(min(ahead, 0.01))
                if gen != self._gen:
                    return
                late = clock.now() - vt
                now = time.monotonic()
                if late < 2 * step / speed * speed or now - last_emit > 0.2:   # drop frames we're too late for
                    img = QImage(data, pw, ph, 3 * pw, QImage.Format_RGB888).copy()
                    self.frame.emit(img, vt)
                    last_emit = now
                k += 1
                data = proc.stdout.read(size)
                if len(data) < size:
                    if gen == self._gen:
                        with self._cv:
                            self._play = None
                        self.audio.stop()
                        self.ended.emit()
                    return
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.wait()


# ---------------------------------------------------------------- widgets
from PySide6.QtCore import QPointF, QSize                                      # noqa: E402
from PySide6.QtGui import QIcon, QPen, QPixmap                                 # noqa: E402
from PySide6.QtWidgets import QLineEdit, QListWidget, QListWidgetItem  # noqa: E402

MEDIA_CATS = ("video", "audio")


def expand_media(paths, limit: int = 1000) -> list[Path]:
    """Files and folders (with subfolders) → the video and audio files in them, sorted by name."""
    import os
    from .util import category
    out: list[Path] = []
    for p in paths:
        p = Path(p)
        if p.is_dir():
            found = []
            for dirpath, dirnames, files in os.walk(p):
                dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and not d.endswith("(converted)"))
                for f in files:
                    if not f.startswith(".") and category(f) in MEDIA_CATS:
                        found.append(Path(dirpath) / f)
                if len(found) >= limit:
                    break
            out += sorted(found, key=lambda x: (str(x.parent).lower(), x.name.lower()))
        elif p.is_file() and category(p) in MEDIA_CATS:
            out.append(p)
    seen, uniq = set(), []
    for p in out:
        k = str(p.resolve()).lower()
        if k not in seen:
            seen.add(k)
            uniq.append(p)
    return uniq[:limit]


class FrameView(QWidget):
    def __init__(self):
        super().__init__()
        self.img: QImage | None = None
        self.flash = 0.0
        self.message = "Loading…"
        self.badge = ""
        self.wave = False              # audio: picture is a waveform, draw the playhead + range on it
        self.head = 0.0
        self.range: tuple[float, float] | None = None
        self.setMinimumSize(420, 236)
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
            if self.wave:
                target = r.adjusted(16, 24, -16, -24)
            else:
                iw, ih = self.img.width(), self.img.height()
                k = min(r.width() / iw, r.height() / ih)
                w, h = iw * k, ih * k
                target = QRectF(r.x() + (r.width() - w) / 2, r.y() + (r.height() - h) / 2, w, h)
            p.drawImage(target, self.img)
            if self.wave:
                if self.range:
                    a, b = self.range
                    p.fillRect(QRectF(target.x(), target.y(), target.width() * a, target.height()), QColor(0, 0, 0, 140))
                    p.fillRect(QRectF(target.x() + target.width() * b, target.y(), target.width() * (1 - b),
                                      target.height()), QColor(0, 0, 0, 140))
                x = target.x() + target.width() * self.head
                p.setPen(QPen(QColor(255, 255, 255, 230), 2))
                p.drawLine(QPointF(x, target.y() - 8), QPointF(x, target.bottom() + 8))
            if self.flash > 0:
                p.fillRect(target, QColor(255, 255, 255, int(200 * self.flash)))
        if self.badge:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, 160))
            br = QRectF(r.x() + 12, r.y() + 12, 8 + 7 * len(self.badge), 24)
            p.drawRoundedRect(br, 6, 6)
            p.setPen(QColor(255, 255, 255, 230))
            p.drawText(br, Qt.AlignCenter, self.badge)
        if self.message:
            p.setPen(QColor(255, 255, 255, 180))
            p.drawText(r.adjusted(20, 20, -20, -20), Qt.AlignCenter | Qt.TextWordWrap, self.message)


class Timeline(QWidget):
    """Scrub bar with a trim range: drag anywhere to scrub, drag the ⟦ ⟧ handles to move In/Out.
    Speaks the QSlider subset the player uses (value, setValue, setRange, isSliderDown, …)."""
    valueChanged = Signal(int)
    sliderReleased = Signal()
    rangeChanged = Signal()

    def __init__(self):
        super().__init__()
        self.lo, self.hi, self._v = 0, 100, 0
        self.tin: int | None = None
        self.tout: int | None = None
        self.down = False
        self.drag: str | None = None     # "head", "in", "out"
        self.setMinimumHeight(40)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    # QSlider-ish API
    def value(self) -> int:
        return self._v

    def setValue(self, v: int):
        v = max(self.lo, min(self.hi, int(v)))
        if v != self._v:
            self._v = v
            self.update()
            if not self.signalsBlocked():
                self.valueChanged.emit(v)

    def setRange(self, lo: int, hi: int):
        self.lo, self.hi = lo, max(lo, hi)
        self._v = max(lo, min(self.hi, self._v))
        self.update()

    def setPageStep(self, _):
        pass

    def isSliderDown(self) -> bool:
        return self.down and self.drag == "head"

    def set_range(self, tin: int | None, tout: int | None):
        self.tin, self.tout = tin, tout
        self.update()

    # geometry
    def _track(self) -> QRectF:
        return QRectF(10, self.height() / 2 - 4, self.width() - 20, 8)

    def _x(self, v: int) -> float:
        t = self._track()
        return t.x() + t.width() * (v - self.lo) / max(1, self.hi - self.lo)

    def _v_at(self, x: float) -> int:
        t = self._track()
        return int(round(self.lo + (x - t.x()) / max(1.0, t.width()) * (self.hi - self.lo)))

    def mousePressEvent(self, e):
        x = e.position().x()
        self.down = True
        self.drag = "head"
        for name, v in (("in", self.tin), ("out", self.tout)):
            if v is not None and abs(self._x(v) - x) < 7:
                self.drag = name
                break
        self._move(x)

    def mouseMoveEvent(self, e):
        x = e.position().x()
        if self.down:
            self._move(x)
            return
        near = any(v is not None and abs(self._x(v) - x) < 7 for v in (self.tin, self.tout))
        self.setCursor(Qt.SizeHorCursor if near else Qt.PointingHandCursor)

    def _move(self, x: float):
        v = max(self.lo, min(self.hi, self._v_at(x)))
        if self.drag == "in":
            self.tin = min(v, self.tout - 1) if self.tout is not None else v
            self.rangeChanged.emit()
            self.setValue(self.tin)
        elif self.drag == "out":
            self.tout = max(v, self.tin + 1) if self.tin is not None else v
            self.rangeChanged.emit()
            self.setValue(self.tout)
        else:
            self.setValue(v)
        self.update()

    def mouseReleaseEvent(self, e):
        was = self.drag
        self.down = False
        self.drag = None
        if was:
            self.sliderReleased.emit()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        t = self._track()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(t, 4, 4)
        if self.tin is not None or self.tout is not None:
            a = self._x(self.tin if self.tin is not None else self.lo)
            b = self._x(self.tout if self.tout is not None else self.hi)
            p.setBrush(QColor(255, 196, 64, 150))
            p.drawRect(QRectF(a, t.y() - 3, b - a, t.height() + 6))
            p.setPen(QPen(QColor(255, 206, 90), 3, Qt.SolidLine, Qt.FlatCap))
            for v, d in ((self.tin, 1), (self.tout, -1)):
                if v is None:
                    continue
                x = self._x(v)
                p.drawLine(QPointF(x, t.y() - 9), QPointF(x, t.bottom() + 9))
                p.drawLine(QPointF(x, t.y() - 9), QPointF(x + 5 * d, t.y() - 9))
                p.drawLine(QPointF(x, t.bottom() + 9), QPointF(x + 5 * d, t.bottom() + 9))
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(140, 180, 255, 200))
        p.drawRoundedRect(QRectF(t.x(), t.y(), self._x(self._v) - t.x(), t.height()), 4, 4)
        x = self._x(self._v)
        p.setBrush(QColor(255, 255, 255))
        p.drawEllipse(QPointF(x, t.center().y()), 8, 8)


def fmt_time(t: float, fps: float = 0) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    ms = int(round((t - int(t)) * 1000)) % 1000
    base = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
    return f"{base}.{ms:03d}"


class ValueBox(QLineEdit):
    """A number you can click and type into (Enter or leaving the field applies it). Accepts 0,7 and 0.7."""
    valueChanged = Signal(float)

    def __init__(self, lo: float, hi: float, decimals: int, suffix: str):
        super().__init__()
        self.lo, self.hi, self.dec, self.suffix = lo, hi, decimals, suffix
        self._v = 0.0
        self.setFixedWidth(76)
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setToolTip("Click and type a value, Enter to apply")
        self.editingFinished.connect(self._commit)

    def value(self) -> float:
        return self._v

    def setValue(self, v: float):
        v = max(self.lo, min(self.hi, float(v)))
        changed = abs(v - self._v) > 1e-9
        self._v = v
        self.setText(self._fmt(v))
        if changed and not self.signalsBlocked():
            self.valueChanged.emit(v)

    def _fmt(self, v: float) -> str:
        txt = f"{v:+.{self.dec}f}" if self.lo < 0 and v else f"{v:.{self.dec}f}"
        return txt + self.suffix

    def focusInEvent(self, e):
        super().focusInEvent(e)
        self.setText(f"{self._v:.{self.dec}f}")     # plain number while typing
        QTimer.singleShot(0, self.selectAll)

    def _commit(self):
        raw = self.text().replace(self.suffix.strip(), "").replace(",", ".").replace("%", "").replace("EV", "").strip()
        try:
            v = float(raw)
        except ValueError:
            self.setText(self._fmt(self._v))
            return
        self.setValue(v)
        self.setText(self._fmt(self._v))


class SliderRow:
    """Label · slider · value box (click it and type a number); double-click the label to reset."""

    def __init__(self, grid: QGridLayout, r: int, label: str, lo: int, hi: int, default: int, scale: float,
                 suffix: str, decimals: int, on_change, on_release):
        self.default, self.scale = default, scale
        self.lab = QLabel(label)
        self.lab.setToolTip("Double-click to reset")
        self.s = QSlider(Qt.Horizontal)
        self.s.setRange(lo, hi)
        self.s.setValue(default)
        self.s.setFocusPolicy(Qt.NoFocus)
        self.box = ValueBox(lo * scale, hi * scale, decimals, suffix)
        self.box.blockSignals(True)
        self.box.setValue(default * scale)
        self.box.blockSignals(False)
        self.s.valueChanged.connect(self._from_slider)
        self.s.valueChanged.connect(lambda _: on_change())
        self.s.sliderReleased.connect(on_release)
        self.box.valueChanged.connect(lambda v: self._from_box(v, on_release))
        self.lab.mouseDoubleClickEvent = lambda e: (self.s.setValue(self.default), on_release())
        grid.addWidget(self.lab, r, 0)
        grid.addWidget(self.s, r, 1)
        grid.addWidget(self.box, r, 2)

    def _from_slider(self, v: int):
        self.box.blockSignals(True)
        self.box.setValue(v * self.scale)
        self.box.blockSignals(False)

    def _from_box(self, v: float, on_release):
        iv = int(round(v / self.scale))
        if iv != self.s.value():
            self.s.blockSignals(True)
            self.s.setValue(iv)
            self.s.blockSignals(False)
        on_release()

    def set(self, v: float):
        self.s.blockSignals(True)
        self.s.setValue(int(round(v)))
        self.s.blockSignals(False)
        self._from_slider(self.s.value())


class ThumbLoader(QObject):
    """Makes the clip-list pictures one by one in the background."""
    ready = Signal(str, QImage, str)    # path, picture, "0:12 · 3840×2160"

    def __init__(self):
        super().__init__()
        self.queue: list[Path] = []
        self.cv = threading.Condition()
        self.stop = False
        threading.Thread(target=self._run, daemon=True).start()

    def add(self, paths):
        with self.cv:
            self.queue += list(paths)
            self.cv.notify()

    def close(self):
        with self.cv:
            self.stop = True
            self.cv.notify()

    def _run(self):
        while True:
            with self.cv:
                while not self.queue and not self.stop:
                    self.cv.wait()
                if self.stop:
                    return
                p = self.queue.pop(0)
            try:
                info = media.probe(p)
                img = thumbnail_image(p, info) or QImage()
                bits = [media.fmt_duration(info.duration)]
                if info.has_video:
                    w, h = info.display_size
                    bits.append(f"{w}×{h}")
                else:
                    bits.append("audio")
                self.ready.emit(str(p), img, " · ".join(b for b in bits if b))
            except Exception:
                self.ready.emit(str(p), QImage(), "can't read")


def make_window(paths, notify, mode: str = "player"):
    from .window import GlassWindow

    class _Win(GlassWindow):
        is_player = True

        def __init__(self):
            super().__init__("Video player", size=(1440, 860))
            self.paths: list[Path] = []
            self.notify = notify
            self.submit = None          # set by the app: background jobs with progress + cancel
            self.idx = -1
            self.path: Path | None = None
            self.src = None
            self.info = None
            self.audio_mode = False
            self.grades: dict[str, G.Grade] = {}
            self.grade = G.last_grade()
            self.saved: list[Path] = []
            self.playing = False
            self.raw: np.ndarray | None = None
            self.raw_hdr = True
            self.compare = False
            self.loop = False
            self.stop_at_out = False
            self.tin: int | None = None
            self.tout: int | None = None
            self.fps, self.sec, self.dur, self.frames, self.n, self.t = 25.0, 25, 0.0, 1, 0, 0.0
            self.setAcceptDrops(True)
            self.source: FrameSource | None = None
            self.audio = AudioPlayer()          # audio files only (video has its own inside FrameSource)
            self.aclock: Clock | None = None
            self.atimer = QTimer(self, interval=30, timeout=self._audio_tick)
            self.thumbs = ThumbLoader()
            self.thumbs.ready.connect(self._thumb_ready)

            top = QHBoxLayout()
            self.sub = QLabel()
            self.sub.setObjectName("dim")
            top.addWidget(self.sub, 1)
            self.prev_clip = QPushButton("◀")
            self.prev_clip.setToolTip("Previous file  (Page Up)")
            self.prev_clip.setFocusPolicy(Qt.NoFocus)
            self.prev_clip.clicked.connect(lambda: self.open_clip(self.idx - 1))
            self.clip_lab = QLabel()
            self.next_clip = QPushButton("▶")
            self.next_clip.setToolTip("Next file  (Page Down)")
            self.next_clip.setFocusPolicy(Qt.NoFocus)
            self.next_clip.clicked.connect(lambda: self.open_clip(self.idx + 1))
            for w in (self.prev_clip, self.clip_lab, self.next_clip):
                top.addWidget(w)
            self.body.addLayout(top)

            main = QHBoxLayout()
            main.setSpacing(12)

            # ---- clip list
            side = QVBoxLayout()
            side.setSpacing(6)
            sh = QHBoxLayout()
            lab = QLabel("FILES")
            lab.setObjectName("section")
            sh.addWidget(lab, 1)
            add = QPushButton("＋")
            add.setToolTip("Add videos or audio  (or drop files / a folder on the window)")
            add.setFocusPolicy(Qt.NoFocus)
            add.clicked.connect(self._add_dialog)
            sh.addWidget(add)
            addf = QPushButton("📁")
            addf.setToolTip("Add a whole folder")
            addf.setFocusPolicy(Qt.NoFocus)
            addf.clicked.connect(self._add_folder_dialog)
            sh.addWidget(addf)
            side.addLayout(sh)
            self.list = QListWidget()
            self.list.setIconSize(QSize(112, 63))
            self.list.setFocusPolicy(Qt.NoFocus)
            self.list.setStyleSheet("QListWidget { background: rgba(255,255,255,0.05); border: none; border-radius: 10px; }"
                                    "QListWidget::item { padding: 5px; border-radius: 8px; }"
                                    "QListWidget::item:selected { background: rgba(140,180,255,0.30); }")
            self.list.currentRowChanged.connect(lambda r: self.open_clip(r) if r >= 0 and r != self.idx else None)
            side.addWidget(self.list, 1)
            self.sidebar = QWidget()
            self.sidebar.setLayout(side)
            self.sidebar.setFixedWidth(300)
            main.addWidget(self.sidebar)

            # ---- picture + transport
            left = QVBoxLayout()
            self.view = FrameView()
            left.addWidget(self.view, 1)

            row = QHBoxLayout()
            self.slider = Timeline()
            self.slider.valueChanged.connect(self._slid)
            self.slider.sliderReleased.connect(self._timeline_released)
            self.slider.rangeChanged.connect(self._range_dragged)
            row.addWidget(self.slider, 1)
            self.clock = QLabel()
            self.clock.setMinimumWidth(230)
            self.clock.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.clock.setStyleSheet("font-family: Consolas, 'Cascadia Mono', monospace;")
            row.addWidget(self.clock)
            left.addLayout(row)

            bar = QHBoxLayout()
            bar.setSpacing(6)

            def btn(text, tip, fn, key=None, into=bar):
                b = QPushButton(text)
                b.setToolTip(tip + (f"  ({key})" if key else ""))
                b.setFocusPolicy(Qt.NoFocus)
                b.clicked.connect(fn)
                into.addWidget(b)
                return b

            btn("−10 s", "Back 10 seconds", lambda: self.step(-10 * self.sec), "Shift+↑")
            btn("−1 s", "Back 1 second", lambda: self.step(-self.sec), "Shift+←")
            self.prev_btn = btn("◀ 1", "Previous frame", lambda: self.step(-1), "←")
            self.play_btn = btn("▶  Play", "Play / pause", self.toggle_play, "Space")
            self.play_btn.setMinimumWidth(96)
            self.next_btn = btn("1 ▶", "Next frame", lambda: self.step(1), "→")
            btn("+1 s", "Forward 1 second", lambda: self.step(self.sec), "Shift+→")
            btn("+10 s", "Forward 10 seconds", lambda: self.step(10 * self.sec), "Shift+↓")
            self.speed = QComboBox()
            self.speed.setFocusPolicy(Qt.NoFocus)
            for s in SPEEDS:
                self.speed.addItem(f"{s}×", s)
            self.speed.setToolTip("Playback speed  (1–5). Sound up to 2×.")
            self.speed.currentIndexChanged.connect(self._speed_changed)
            bar.addWidget(self.speed)
            self.mute_btn = btn("🔊", "Sound on/off", self._toggle_mute, "M")
            bar.addStretch(1)
            self.color_btn = QPushButton("🎨  Color")
            self.color_btn.setCheckable(True)
            self.color_btn.setFocusPolicy(Qt.NoFocus)
            self.color_btn.setToolTip("LUT, exposure, contrast, presets, export  (C)")
            self.color_btn.toggled.connect(self._toggle_panel)
            bar.addWidget(self.color_btn)
            self.fmt = QComboBox()
            self.fmt.setFocusPolicy(Qt.NoFocus)
            self.fmt.addItem("PNG", "PNG")
            self.fmt.addItem("JPG", "JPG")
            self.fmt.setToolTip("Screenshot format: PNG lossless, JPG smaller")
            self.fmt.setCurrentIndex(1 if CONFIG.get("frame_format") == "JPG" else 0)
            self.fmt.currentIndexChanged.connect(lambda _: CONFIG.__setitem__("frame_format", self.fmt.currentData()))
            bar.addWidget(self.fmt)
            self.shot_btn = QPushButton("📷  Screenshot")
            self.shot_btn.setObjectName("primary")
            self.shot_btn.setToolTip("Save this frame in full resolution, with the grade  (S or Enter)")
            self.shot_btn.setFocusPolicy(Qt.NoFocus)
            self.shot_btn.clicked.connect(self.screenshot)
            bar.addWidget(self.shot_btn)
            left.addLayout(bar)

            # ---- trim row
            trim = QHBoxLayout()
            trim.setSpacing(6)
            tl = QLabel("TRIM")
            tl.setObjectName("section")
            trim.addWidget(tl)
            btn("⟦ In", "Set the start here", self.set_in, "I", trim)
            btn("Out ⟧", "Set the end here", self.set_out, "O", trim)
            btn("▶ In→Out", "Play the trimmed part", self.play_range, "P", trim)
            self.loop_btn = QPushButton("🔁 Loop")
            self.loop_btn.setCheckable(True)
            self.loop_btn.setFocusPolicy(Qt.NoFocus)
            self.loop_btn.setToolTip("Repeat In→Out while playing  (L)")
            self.loop_btn.toggled.connect(self._set_loop)
            trim.addWidget(self.loop_btn)
            btn("✕", "Clear In and Out", self.clear_range, "X", trim)
            self.range_lab = QLabel("Set In and Out with I and O, or drag the yellow handles")
            self.range_lab.setObjectName("dim")
            trim.addWidget(self.range_lab, 1)
            self.trim_mode = QComboBox()
            self.trim_mode.setFocusPolicy(Qt.NoFocus)
            trim.addWidget(self.trim_mode)
            self.trim_btn = QPushButton("✂  Save trim")
            self.trim_btn.setObjectName("primary")
            self.trim_btn.setFocusPolicy(Qt.NoFocus)
            self.trim_btn.setToolTip("Save In→Out as a new file next to the original")
            self.trim_btn.clicked.connect(self.save_trim)
            trim.addWidget(self.trim_btn)
            left.addLayout(trim)
            main.addLayout(left, 1)

            self.panel = self._build_panel()
            main.addWidget(self.panel)
            self.body.addLayout(main, 1)

            foot = QHBoxLayout()
            self.status = QLabel("← → frame by frame · Shift for 1 s · Space plays · S screenshot · I / O trim · C colour")
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
                    ("Shift+Left", lambda: self.step(-self.sec)), ("Shift+Right", lambda: self.step(self.sec)),
                    ("Shift+Up", lambda: self.step(-10 * self.sec)), ("Shift+Down", lambda: self.step(10 * self.sec)),
                    (Qt.Key_Comma, lambda: self.step(-1)), (Qt.Key_Period, lambda: self.step(1)),
                    (Qt.Key_Space, self.toggle_play), (Qt.Key_K, self.toggle_play),
                    (Qt.Key_S, self.screenshot), (Qt.Key_Return, self.screenshot), (Qt.Key_Enter, self.screenshot),
                    (Qt.Key_M, self._toggle_mute), (Qt.Key_C, lambda: self.color_btn.toggle()),
                    (Qt.Key_I, self.set_in), (Qt.Key_O, self.set_out), (Qt.Key_P, self.play_range),
                    (Qt.Key_L, lambda: self.loop_btn.toggle()), (Qt.Key_X, self.clear_range),
                    (Qt.Key_PageUp, lambda: self.open_clip(self.idx - 1)),
                    (Qt.Key_PageDown, lambda: self.open_clip(self.idx + 1)),
                    (Qt.Key_Home, lambda: self.goto(0)), (Qt.Key_End, lambda: self.goto(self.frames - 1))]
            keys += [(getattr(Qt, f"Key_{i + 1}"), (lambda i=i: self.speed.setCurrentIndex(i))) for i in range(len(SPEEDS))]
            for k, fn in keys:
                QShortcut(QKeySequence(k), self, activated=fn)

            # grade changes: instant on the still, debounced for playback
            self._regrade = QTimer(self, singleShot=True, interval=30, timeout=self._apply_grade_still)
            self._replay = QTimer(self, singleShot=True, interval=250, timeout=self._restart_play)
            self._persist = QTimer(self, singleShot=True, interval=800, timeout=lambda: G.remember_last(self.grade))
            _has_filter("zscale")
            self._fast_still = False
            self._load_grade_ui()
            panel_open = CONFIG.get("player_color_panel")
            self.color_btn.setChecked(bool(panel_open) if panel_open is not None else self.grade.profile in G.LOG_PROFILES)
            self.panel.setVisible(self.color_btn.isChecked())
            self._update_nav()
            if mode == "trim":
                self.setWindowTitle("Trim — DragShift")
            self.add_paths(paths)
            if not self.paths:
                self.view.message = ("Drop videos, audio files or a whole folder here\n"
                                     "or click ＋ / 📁 on the left")
                self.view.update()

        # ================================================ files
        def add_paths(self, paths, select: bool = True):
            new = [p for p in expand_media(paths) if all(str(p.resolve()).lower() != str(q.resolve()).lower()
                                                       for q in self.paths)]
            first_new = len(self.paths)
            for p in new:
                self.paths.append(p)
                it = QListWidgetItem(p.name + "\n…")
                it.setToolTip(str(p))
                it.setSizeHint(QSize(260, 74))
                pm = QPixmap(112, 63)
                pm.fill(QColor(0, 0, 0, 90))
                it.setIcon(QIcon(pm))
                self.list.addItem(it)
            self.thumbs.add(new)
            self.export_all.setText(f"Export all {len(self.paths)}")
            self.export_all.setVisible(len(self.paths) > 1)
            self._update_nav()
            if new and select:
                self.open_clip(first_new if self.idx >= 0 else 0)
            elif not new and paths and not self.paths:
                self.status.setText("No video or audio files found there.")
            return new

        def _thumb_ready(self, path: str, img: QImage, meta: str):
            for i, p in enumerate(self.paths):
                if str(p) == path:
                    it = self.list.item(i)
                    if it is None:
                        return
                    it.setText(f"{p.name}\n{meta}")
                    if not img.isNull():
                        pm = QPixmap.fromImage(img).scaled(112, 63, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
                        it.setIcon(QIcon(pm.copy(0, 0, 112, 63)))
                    return

        def _add_dialog(self):
            files, _ = QFileDialog.getOpenFileNames(self, "Add videos or audio", str(self.path.parent) if self.path else "",
                                                    "Video & audio (*.mp4 *.mov *.mkv *.mxf *.avi *.mts *.m2ts *.webm "
                                                    "*.wav *.mp3 *.flac *.m4a *.aac *.ogg *.opus *.aif *.aiff);;All files (*)")
            if files:
                self.add_paths(files)

        def _add_folder_dialog(self):
            d = QFileDialog.getExistingDirectory(self, "Add a folder", str(self.path.parent) if self.path else "")
            if d:
                if not self.add_paths([d]):
                    self.status.setText("No video or audio files in that folder.")

        def _update_nav(self):
            n = len(self.paths)
            for w in (self.prev_clip, self.clip_lab, self.next_clip):
                w.setVisible(n > 1)
            self.clip_lab.setText(f"{self.idx + 1} / {n}" if n else "")
            self.prev_clip.setEnabled(self.idx > 0)
            self.next_clip.setEnabled(0 <= self.idx < n - 1)

        # ================================================ colour panel
        def _build_panel(self) -> QWidget:
            panel = QFrame()
            panel.setObjectName("colorPanel")
            panel.setStyleSheet("QFrame#colorPanel { background: rgba(255,255,255,0.05); border-radius: 12px; }")
            panel.setFixedWidth(360)
            outer = QVBoxLayout(panel)
            outer.setContentsMargins(0, 0, 0, 0)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }"
                                 "QScrollArea > QWidget > QWidget { background: transparent; }")
            inner = QWidget()
            v = QVBoxLayout(inner)
            v.setContentsMargins(14, 12, 14, 12)
            v.setSpacing(8)

            def head(t):
                l = QLabel(t.upper())
                l.setObjectName("section")
                v.addWidget(l)

            head("Preset")
            pr = QHBoxLayout()
            self.preset_box = QComboBox()
            self.preset_box.setFocusPolicy(Qt.NoFocus)
            self.preset_box.activated.connect(self._preset_chosen)
            pr.addWidget(self.preset_box, 1)
            save = QPushButton("Save")
            save.setToolTip("Save LUT + all sliders as a preset")
            save.setFocusPolicy(Qt.NoFocus)
            save.clicked.connect(self._save_preset)
            pr.addWidget(save)
            self.del_preset = QPushButton("✕")
            self.del_preset.setObjectName("chrome")
            self.del_preset.setToolTip("Delete the selected preset")
            self.del_preset.setFocusPolicy(Qt.NoFocus)
            self.del_preset.clicked.connect(self._delete_preset)
            pr.addWidget(self.del_preset)
            v.addLayout(pr)

            head("Camera")
            self.profile_box = QComboBox()
            self.profile_box.setFocusPolicy(Qt.NoFocus)
            for k, name in G.PROFILES.items():
                self.profile_box.addItem(name, k)
            self.profile_box.setToolTip("What the camera recorded. Log profiles make exposure and the built-in "
                                        "conversion work in real stops.")
            self.profile_box.currentIndexChanged.connect(self._profile_changed)
            v.addWidget(self.profile_box)

            head("LUT")
            self.lut_box = QComboBox()
            self.lut_box.setFocusPolicy(Qt.NoFocus)
            self.lut_box.activated.connect(self._lut_chosen)
            v.addWidget(self.lut_box)
            hint = QLabel("Drop a .cube file on the window to use it")
            hint.setObjectName("dim")
            v.addWidget(hint)
            grid = QGridLayout()
            grid.setHorizontalSpacing(8)
            grid.setVerticalSpacing(6)
            v.addLayout(grid)
            self.rows = {}
            spec = [("strength", "LUT amount", 0, 100, 100, 1, " %", 0),
                    ("exposure", "Exposure", -40, 40, 0, 0.1, " EV", 1),
                    ("contrast", "Contrast", -100, 100, 0, 1, "", 0),
                    ("highlights", "Highlights", -100, 100, 0, 1, "", 0),
                    ("shadows", "Shadows", -100, 100, 0, 1, "", 0),
                    ("saturation", "Saturation", 0, 200, 100, 1, " %", 0),
                    ("temperature", "Temperature", -100, 100, 0, 1, "", 0),
                    ("tint", "Tint", -100, 100, 0, 1, "", 0)]
            for i, (key, label, lo, hi, d, scale, suffix, dec) in enumerate(spec):
                self.rows[key] = SliderRow(grid, i, label, lo, hi, d, scale, suffix, dec,
                                           self._slider_moved, self._slider_done)
            cmp_row = QHBoxLayout()
            self.cmp_btn = QPushButton("Hold: original")
            self.cmp_btn.setToolTip("Press and hold to see the ungraded picture")
            self.cmp_btn.setFocusPolicy(Qt.NoFocus)
            self.cmp_btn.pressed.connect(lambda: self._set_compare(True))
            self.cmp_btn.released.connect(lambda: self._set_compare(False))
            cmp_row.addWidget(self.cmp_btn)
            reset = QPushButton("Reset")
            reset.setFocusPolicy(Qt.NoFocus)
            reset.setToolTip("Sliders back to zero (keeps camera and LUT)")
            reset.clicked.connect(self._reset_sliders)
            cmp_row.addWidget(reset)
            v.addLayout(cmp_row)

            head("Export")
            self.export_box = QComboBox()
            self.export_box.setFocusPolicy(Qt.NoFocus)
            for k, (name, _) in EXPORTS.items():
                self.export_box.addItem(name, k)
            self.export_box.currentIndexChanged.connect(lambda _: self._fill_trim_modes())
            v.addWidget(self.export_box)
            ex = QHBoxLayout()
            self.export_btn = QPushButton("Export video")
            self.export_btn.setObjectName("primary")
            self.export_btn.setFocusPolicy(Qt.NoFocus)
            self.export_btn.setToolTip("The whole clip with this grade, saved next to the original")
            self.export_btn.clicked.connect(lambda: self.export(all_clips=False))
            ex.addWidget(self.export_btn)
            self.export_all = QPushButton("Export all")
            self.export_all.setFocusPolicy(Qt.NoFocus)
            self.export_all.setToolTip("Every video in the list, each with its own grade (unvisited ones use this one)")
            self.export_all.clicked.connect(lambda: self.export(all_clips=True))
            ex.addWidget(self.export_all)
            v.addLayout(ex)
            v.addStretch(1)
            scroll.setWidget(inner)
            outer.addWidget(scroll)
            return panel

        def _load_grade_ui(self):
            g = self.grade
            self.profile_box.blockSignals(True)
            self.profile_box.setCurrentIndex(max(0, self.profile_box.findData(g.profile)))
            self.profile_box.blockSignals(False)
            self._fill_lut_box()
            vals = {"strength": g.strength * 100, "exposure": g.exposure * 10, "contrast": g.contrast * 100,
                    "highlights": g.highlights * 100, "shadows": g.shadows * 100, "saturation": g.saturation * 100,
                    "temperature": g.temperature * 100, "tint": g.tint * 100}
            for k, val in vals.items():
                self.rows[k].set(val)
            self._fill_presets()

        def _fill_lut_box(self):
            b = self.lut_box
            b.blockSignals(True)
            b.clear()
            b.addItem("No LUT", G.NO_LUT)
            if self.grade.profile in G.LOG_PROFILES:
                b.addItem("Camera → Rec.709 (built in)", G.BUILTIN)
            recent = G.recent_luts()
            if self.grade.lut not in (G.NO_LUT, G.BUILTIN) and self.grade.lut not in recent:
                recent = [self.grade.lut] + recent
            for p in recent:
                b.addItem(Path(p).stem, p)
                b.setItemData(b.count() - 1, p, Qt.ToolTipRole)
            b.addItem("Load LUT…", "__browse__")
            i = b.findData(self.grade.lut)
            b.setCurrentIndex(i if i >= 0 else 0)
            b.blockSignals(False)

        def _fill_presets(self, select_id: str | None = None):
            b = self.preset_box
            b.blockSignals(True)
            b.clear()
            b.addItem("— choose a preset —", None)
            for p in G.presets():
                b.addItem(p["name"], p["id"])
            if select_id:
                b.setCurrentIndex(max(0, b.findData(select_id)))
            b.blockSignals(False)
            self.del_preset.setEnabled(b.currentData() is not None)

        def _preset_chosen(self, i: int):
            pid = self.preset_box.itemData(i)
            self.del_preset.setEnabled(pid is not None)
            if pid is None:
                return
            p = next((p for p in G.presets() if p["id"] == pid), None)
            if not p:
                return
            g = G.Grade.from_dict(p["grade"])
            if g.lut not in (G.NO_LUT, G.BUILTIN) and not Path(g.lut).exists():
                self.status.setText(f"LUT of this preset not found: {g.lut}")
                g.lut = G.BUILTIN if g.profile in G.LOG_PROFILES else G.NO_LUT
            self.set_grade(g)
            self._load_grade_ui()
            self.preset_box.setCurrentIndex(i)
            self.del_preset.setEnabled(True)
            self.status.setText(f"Preset “{p['name']}” applied — fine-tune exposure if needed")

        def _save_preset(self):
            name, ok = QInputDialog.getText(self, "Save preset", "Name (e.g. “Interview FX3 Studio”):")
            if not ok or not name.strip():
                return
            e = G.save_preset(name, self.grade)
            self._fill_presets(e["id"])
            self.status.setText(f"Preset “{e['name']}” saved")

        def _delete_preset(self):
            pid = self.preset_box.currentData()
            if pid:
                G.delete_preset(pid)
                self._fill_presets()

        def _profile_changed(self, *_):
            prof = self.profile_box.currentData()
            g = replace(self.grade, profile=prof)
            if prof in G.LOG_PROFILES and g.lut == G.NO_LUT:
                g.lut = G.BUILTIN              # log footage looks flat until it's converted
            if prof not in G.LOG_PROFILES and g.lut == G.BUILTIN:
                g.lut = G.NO_LUT
            self.set_grade(g)
            self._fill_lut_box()

        def _lut_chosen(self, i: int):
            data = self.lut_box.itemData(i)
            if data == "__browse__":
                start = str(Path(self.grade.lut).parent) if self.grade.lut not in (G.NO_LUT, G.BUILTIN) else ""
                f, _ = QFileDialog.getOpenFileName(self, "Choose a LUT", start, "LUT (*.cube)")
                if f:
                    self.use_lut(f)
                else:
                    self._fill_lut_box()
                return
            self.set_grade(replace(self.grade, lut=data or G.NO_LUT))
            self._fill_lut_box()

        def use_lut(self, path: str):
            try:
                G.read_cube(path)
            except UserError as e:
                self.status.setText(str(e))
                self._fill_lut_box()
                return
            G.remember_lut(path)
            self.set_grade(replace(self.grade, lut=str(Path(path).resolve())))
            self._fill_lut_box()
            self.status.setText(f"LUT {Path(path).stem} applied")
            if not self.color_btn.isChecked() and not self.audio_mode:
                self.color_btn.setChecked(True)

        def _grade_from_sliders(self) -> G.Grade:
            r = {k: row.s.value() for k, row in self.rows.items()}
            return replace(self.grade, strength=r["strength"] / 100, exposure=r["exposure"] / 10,
                           contrast=r["contrast"] / 100, highlights=r["highlights"] / 100,
                           shadows=r["shadows"] / 100, saturation=r["saturation"] / 100,
                           temperature=r["temperature"] / 100, tint=r["tint"] / 100)

        def _slider_moved(self):
            self._fast_still = True
            self.set_grade(self._grade_from_sliders(), quick=True)

        def _slider_done(self):
            self._fast_still = False
            self.set_grade(self._grade_from_sliders())

        def _reset_sliders(self):
            self.set_grade(G.Grade(profile=self.grade.profile, lut=self.grade.lut))
            self._load_grade_ui()

        def set_grade(self, g: G.Grade, quick: bool = False):
            if g == self.grade and not quick:
                self._regrade.start()
                return
            self.grade = g
            if self.path is not None:
                self.grades[str(self.path)] = g
            self.preset_box.blockSignals(True)
            self.preset_box.setCurrentIndex(0)
            self.preset_box.blockSignals(False)
            self.del_preset.setEnabled(False)
            self._persist.start()
            self._regrade.start()
            if self.playing:
                self._replay.start()

        def _set_compare(self, on: bool):
            self.compare = on
            self.view.badge = "Original" if on else ""
            if self.playing:
                self._restart_play()
            else:
                self._apply_grade_still()

        def active_grade(self) -> G.Grade | None:
            return None if self.compare else self.grade

        def _apply_grade_still(self):
            if self.raw is None or self.playing or self.audio_mode:
                return
            g = self.active_grade()
            graded = g is not None and not g.is_identity()
            want_hdr = not graded
            if is_hdr(self.info) and want_hdr != self.raw_hdr:
                self._request_still(exact=True)
                return
            a = self.raw
            if graded:
                try:
                    if self._fast_still and a.shape[1] > 700:
                        a = np.ascontiguousarray(a[::2, ::2])
                    a = G.apply_to_image(g, a)
                except UserError as e:
                    self.status.setText(str(e))
            self.view.set_image(to_qimage(a))

        def _toggle_panel(self, on: bool):
            self.panel.setVisible(on and not self.audio_mode)
            CONFIG["player_color_panel"] = bool(on)

        # ================================================ clips
        def open_clip(self, i: int):
            if not (0 <= i < len(self.paths)) or (self.idx == i and self.info is not None):
                return
            self.pause()
            if self.path is not None:
                self.grades[str(self.path)] = self.grade
            if self.source is not None:
                self.source.close()
                self.source = None
            path = self.paths[i]
            try:
                info = media.probe(path)
                if not (info.has_video or info.has_audio):
                    raise UserError("No video or audio in this file.")
            except UserError as e:
                if not self.paths or len(self.paths) == 1:
                    raise
                self.status.setText(f"{path.name}: {e}")
                return
            self.idx = i
            self.path = path
            self.src, self.info = path, info
            self.audio_mode = not info.has_video
            if self.audio_mode:
                self.fps = 100.0                       # audio: 1 "frame" = 10 ms
            else:
                self.fps = info.fps if info.fps and info.fps < 1000 else 25.0
            self.sec = max(1, int(round(self.fps)))
            self.dur = info.duration or 0.0
            self.frames = max(1, int(round(self.dur * self.fps)))
            self.n = 0
            self.t = 0.0
            self.raw = None
            self.tin = self.tout = None
            self.slider.set_range(None, None)
            self.slider.blockSignals(True)
            self.slider.setRange(0, self.frames - 1)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            g = self.grades.get(str(path))
            if g is not None and g != self.grade:
                self.grade = g
                self._load_grade_ui()
            self.list.blockSignals(True)
            self.list.setCurrentRow(i)
            self.list.blockSignals(False)
            if self.audio_mode:
                bits = [path.name, media.fmt_duration(self.dur), f"{info.sample_rate // 1000 or '?'} kHz",
                        info.achannels or ""]
                self.sub.setText("  ·  ".join(b for b in bits if b))
            else:
                w, h = info.display_size
                self.sub.setText(f"{path.name}  ·  {w}×{h}  ·  {self.fps:g} fps  ·  {media.fmt_duration(self.dur)}"
                                 + ("  ·  HDR" if is_hdr(info) else "") + ("" if info.has_audio else "  ·  no sound"))
            self.setWindowTitle(f"{path.name} — DragShift")
            for wdg in (self.color_btn, self.fmt, self.shot_btn, self.prev_btn, self.next_btn):
                wdg.setVisible(not self.audio_mode)
            self.panel.setVisible(self.color_btn.isChecked() and not self.audio_mode)
            self._fill_trim_modes()
            self._update_nav()
            self.view.wave = self.audio_mode
            self.view.range = None
            self.view.head = 0.0
            self.view.message = "Loading…"
            self.view.img = None
            self.view.update()
            self._update_range_label()
            if self.audio_mode:
                self._load_wave(path)
            else:
                self.source = FrameSource(path, info)
                self.source.audio.muted = self.mute_btn.text() == "🔇"
                self.source.still.connect(self._on_still)
                self.source.frame.connect(self._on_frame)
                self.source.ended.connect(self._on_end)
                self._request_still(exact=True)
            self._update_clock()

        def _load_wave(self, path: Path):
            holder = {}

            def work():
                holder["img"] = waveform(path)
            th = threading.Thread(target=work, daemon=True)
            th.start()

            def check():
                if th.is_alive():
                    QTimer.singleShot(50, check)
                    return
                if self.path == path:
                    img = holder.get("img")
                    if img is not None:
                        self.view.set_image(img)
                    else:
                        self.view.message = "♪"
                        self.view.update()
            QTimer.singleShot(50, check)

        # ================================================ navigation
        def _request_still(self, exact: bool = True):
            if self.audio_mode or self.source is None:
                return
            g = self.active_grade()
            hdr_map = g is None or g.is_identity()
            self.raw_hdr = hdr_map
            self.source.seek(frame_time(self.n, self.fps), fast=not exact, hdr_map=hdr_map)

        def goto(self, n: int, exact: bool = True):
            if self.info is None:
                return
            self.pause()
            self.n = max(0, min(self.frames - 1, int(n)))
            self.t = self.n / self.fps
            self._set_slider(self.n)
            self._update_clock()
            self._request_still(exact)

        def step(self, k: int):
            self.goto(self.n + k)

        def _slid(self, v: int):
            if getattr(self, "_slide_guard", False):
                return
            self.goto(v, exact=not self.slider.isSliderDown())

        def _timeline_released(self):
            self.goto(self.slider.value(), exact=True)

        def _set_slider(self, n: int):
            self._slide_guard = True
            self.slider.setValue(n)
            self._slide_guard = False
            if self.audio_mode:
                self.view.head = n / max(1, self.frames - 1)
                self.view.update()

        # ================================================ trim
        def set_in(self):
            if self.info is None:
                return
            self.tin = self.n
            if self.tout is not None and self.tout <= self.tin:
                self.tout = None
            self._range_changed()

        def set_out(self):
            if self.info is None:
                return
            self.tout = self.n
            if self.tin is not None and self.tin >= self.tout:
                self.tin = None
            self._range_changed()

        def clear_range(self):
            self.tin = self.tout = None
            self.loop_btn.setChecked(False)
            self._range_changed()

        def _range_dragged(self):
            self.tin, self.tout = self.slider.tin, self.slider.tout
            self._update_range_label()

        def _range_changed(self):
            self.slider.set_range(self.tin, self.tout)
            self._update_range_label()

        def range_seconds(self) -> tuple[float, float] | None:
            if self.tin is None and self.tout is None:
                return None
            a = (self.tin or 0) / self.fps
            b = (self.tout if self.tout is not None else self.frames) / self.fps
            if self.tout is not None and not self.audio_mode:
                b += 1 / self.fps                 # Out is the last frame that stays in
            return a, min(b, self.dur or b)

        def _update_range_label(self):
            r = self.range_seconds()
            if r is None:
                self.range_lab.setText("Set In and Out with I and O, or drag the yellow handles")
                self.view.range = None
            else:
                a, b = r
                self.range_lab.setText(f"In {fmt_time(a)}  ·  Out {fmt_time(b)}  ·  {b - a:.2f} s")
                if self.audio_mode and self.dur:
                    self.view.range = (a / self.dur, b / self.dur)
            self.trim_btn.setEnabled(r is not None)
            self.view.update()

        def _fill_trim_modes(self):
            b = self.trim_mode
            b.blockSignals(True)
            b.clear()
            if self.audio_mode:
                b.addItem("Exact · same format", "audio")
            else:
                label = EXPORTS[self.export_box.currentData()][0]
                b.addItem(f"Exact · {label}", "exact")
                b.addItem("Fast · no re-encode (keyframe)", "fast")
            b.blockSignals(False)
            b.setToolTip("Exact cuts on the frame and applies your colour grade; Fast copies without "
                         "re-encoding but can only start on a keyframe")

        def _set_loop(self, on: bool):
            self.loop = on
            if on and self.tin is None and self.tout is None:
                self.status.setText("Loop repeats the In→Out part – set In and Out first (I / O)")

        def play_range(self):
            r = self.range_seconds()
            if r is None:
                self.status.setText("Set In and Out first (I / O)")
                return
            self.stop_at_out = not self.loop
            self.goto(self.tin or 0)
            self.toggle_play()

        def _check_range(self) -> bool:
            """During playback: loop back to In or stop at Out. True when playback was redirected."""
            if self.tout is None or not (self.loop or self.stop_at_out):
                return False
            if self.n >= self.tout:
                if self.loop:
                    self.n = self.tin or 0
                    self._start_playback()
                else:
                    self.stop_at_out = False
                    self.pause()
                    self.goto(self.tout)
                return True
            return False

        def save_trim(self):
            r = self.range_seconds()
            if r is None or self.path is None:
                return
            a, b = r
            mode = self.trim_mode.currentData()
            src, g, target = self.path, self.grade, self.export_box.currentData()
            if mode == "audio":
                def work():
                    return trim_audio(src, a, b)
            elif mode == "fast":
                def work():
                    return trim_fast(src, a, b)
            else:
                def work():
                    return export_graded(src, g, target, a, b)
            self._run_job("tool:Trim", [str(src)], work, "Trimming…")

        def _run_job(self, action, srcs, work, label):
            if self.submit is not None:
                self.submit(action, srcs, work, label, cancellable=True)
                self.status.setText(f"{label.rstrip('…')} in the background…")
            else:   # standalone (tests)
                try:
                    outs = work()
                    self.status.setText(f"✓ Saved {outs[0].name}")
                    self.last_export = outs
                except Exception as e:  # noqa: BLE001
                    self.status.setText(f"Failed: {e}")
                    self.last_export = []

        # ================================================ playback
        def toggle_play(self):
            if self.info is None:
                return
            if self.playing:
                self.pause()
                self.goto(self.n)
            else:
                if self.n >= self.frames - 1:
                    self.n = (self.tin or 0) if self.loop else 0
                self.playing = True
                self.play_btn.setText("❚❚  Pause")
                self._start_playback()

        def _start_playback(self):
            speed = self.speed.currentData()
            t = self.n / self.fps
            if self.audio_mode:
                self.audio.muted = self.mute_btn.text() == "🔇"
                self.audio.stop()
                self.audio.start(self.src, t, speed, self.info)
                self.aclock = Clock(t, speed, self.audio if self.audio.active else None)
                self.atimer.start()
            else:
                self.source.play(t, speed, self.active_grade() or G.Grade())

        def _restart_play(self):
            if self.playing:
                self._start_playback()

        def pause(self):
            if self.playing:
                self.playing = False
                self.play_btn.setText("▶  Play")
                if self.audio_mode:
                    self.atimer.stop()
                    self.audio.stop()
                elif self.source is not None:
                    self.source.pause()

        def _speed_changed(self, *_):
            self._restart_play()

        def _toggle_mute(self):
            muted = self.mute_btn.text() != "🔇"
            self.mute_btn.setText("🔇" if muted else "🔊")
            if self.source is not None:
                self.source.audio.muted = muted
            self.audio.muted = muted
            self._restart_play()

        def _audio_tick(self):
            if not self.playing or self.aclock is None:
                return
            t = self.aclock.now()
            if t >= self.dur:
                self._on_end()
                return
            self.n = max(0, min(self.frames - 1, int(round(t * self.fps))))
            self._set_slider(self.n)
            self._update_clock()
            self._check_range()

        def _on_still(self, a, t: float, exact: bool):
            if self.playing:
                return
            self.raw = a
            self._apply_grade_still()

        def _on_frame(self, img: QImage, t: float):
            if not self.playing:
                return
            self.view.set_image(img)
            self.t = t
            self.n = max(0, min(self.frames - 1, int(round(t * self.fps))))
            self._set_slider(self.n)
            self._update_clock()
            self._check_range()

        def _on_end(self):
            if self.playing:
                if self.loop and self.tout is None and self.tin is not None:
                    self.n = self.tin
                    self._start_playback()
                    return
                self.pause()
                self.goto(self.frames - 1)

        def _update_clock(self):
            if self.audio_mode:
                self.clock.setText(f"{fmt_time(self.n / self.fps)}  /  {fmt_time(self.dur)}")
            else:
                self.clock.setText(f"{fmt_time(self.n / self.fps)}  ·  frame {self.n + 1} / {self.frames}")

        # ================================================ drop: a LUT, more files or folders
        def dragEnterEvent(self, e):
            urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            if any(f.lower().endswith(".cube") for f in urls) or any(
                    Path(f).is_dir() or Path(f).suffix.lower() in VIDEO_OR_AUDIO for f in urls):
                e.acceptProposedAction()

        def dropEvent(self, e):
            urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
            cube = next((f for f in urls if f.lower().endswith(".cube")), None)
            if cube:
                self.use_lut(cube)
            rest = [f for f in urls if not f.lower().endswith(".cube")]
            if rest:
                new = self.add_paths(rest)
                self.status.setText(f"Added {len(new)} file{'s' if len(new) != 1 else ''}" if new
                                    else "No new video or audio files there")
            e.acceptProposedAction()

        # ================================================ screenshot + export
        def screenshot(self):
            if self.audio_mode or self.info is None:
                return
            was_playing = self.playing
            if was_playing:
                self.pause()
            n, fmt = self.n, self.fmt.currentData()
            t = frame_time(n, self.fps)
            g = self.grade
            src, info = self.src, self.info
            self.view.blink()
            self.shot_btn.setEnabled(False)
            self.status.setText(f"Saving frame {n + 1}…")
            holder: dict = {}

            def work():
                try:
                    holder["out"] = save_frame(src, t, fmt, info, shown_at=n / self.fps, grade=g)
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

        def export(self, all_clips: bool = False):
            target = self.export_box.currentData()
            if self.path is not None:
                self.grades[str(self.path)] = self.grade
            vids = [p for p in (self.paths if all_clips else [self.path]) if p is not None]
            if all_clips:
                from .util import category
                vids = [p for p in vids if category(p) == "video"]
            jobs = [(p, self.grades.get(str(p), self.grade)) for p in vids]
            if not jobs:
                return
            label = EXPORTS[target][0]

            def work():
                return media.run_batch(jobs, lambda j: export_graded(j[0], j[1], target))
            self._run_job("tool:Graded", [str(p) for p, _ in jobs], work, f"Exporting {label}…")

        def _reveal(self):
            from .toasts import reveal
            if self.saved:
                reveal(self.saved[-1])

        def closeEvent(self, e):
            if self.source is not None:
                self.source.close()
            self.audio.stop()
            self.atimer.stop()
            self.thumbs.close()
            G.remember_last(self.grade)
            if self.saved and self.notify:
                try:
                    self.notify("tool:Screenshots", [self.src], list(self.saved), "")
                except Exception:
                    pass
                self.saved = []
            super().closeEvent(e)

    w = _Win()
    if not w.paths and paths:
        w.close()
        raise UserError("No video or audio files found there.")
    return w


from .util import AUDIO_EXTS, VIDEO_EXTS  # noqa: E402
VIDEO_OR_AUDIO = VIDEO_EXTS | AUDIO_EXTS
