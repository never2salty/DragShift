"""Video and audio conversion through FFmpeg.

Every public function takes a source path and returns a list of output paths written next to
the source (the original is never touched). Long jobs report progress and can be cancelled
through `job_context()`, which the app sets up around each background job.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .util import CONFIG, UserError, sibling

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0  # CREATE_NO_WINDOW: no console flash from the tray app

# ---------------------------------------------------------------- formats
VIDEO_TARGETS = ["MP4", "HEVC", "PRORES", "MOV", "MKV", "WEBM", "GIF", "MP3", "WAV"]
AUDIO_TARGETS = ["MP3", "WAV", "FLAC", "M4A", "ALAC", "OGG", "OPUS", "AIFF"]
VIDEO_TOOLS = ["Compress", "Fit size", "1080p", "720p", "Rotate", "Mute", "Thumbnail"]
AUDIO_TOOLS = ["Normalize", "Trim silence", "Mono"]

TARGET_EXT = {"MP4": ".mp4", "HEVC": ".mp4", "PRORES": ".mov", "MOV": ".mov", "MKV": ".mkv",
              "WEBM": ".webm", "GIF": ".gif", "MP3": ".mp3", "WAV": ".wav", "FLAC": ".flac",
              "M4A": ".m4a", "ALAC": ".m4a", "OGG": ".ogg", "OPUS": ".opus", "AIFF": ".aiff"}

# extra word in the output name where the extension alone would be ambiguous ("Clip HEVC.mp4")
TARGET_LABEL = {"HEVC": "HEVC", "PRORES": "ProRes", "ALAC": "ALAC"}

# Camera RAW video that FFmpeg cannot decode (vendor SDKs only). ProRes RAW works with FFmpeg 8+.
UNDECODABLE_RAW = {
    ".braw": "Blackmagic RAW can only be decoded with Blackmagic's own SDK. "
             "Export it from DaVinci Resolve (Deliver → H.264/H.265/ProRes), then drop that file here.",
    ".r3d": "RED R3D can only be decoded with RED's SDK. Export it from REDCINE-X or DaVinci Resolve first.",
    ".ari": "ARRIRAW needs ARRI's SDK. Export it from DaVinci Resolve or ARRI Reference Tool first.",
    ".arx": "ARRIRAW needs ARRI's SDK. Export it from DaVinci Resolve or ARRI Reference Tool first.",
    ".crm": "Canon Cinema RAW Light needs Canon's SDK. Export it from DaVinci Resolve or Cinema RAW Development first.",
    ".nev": "Nikon N-RAW needs Nikon's SDK. Export it from DaVinci Resolve or NX Studio first.",
}

QUALITY = {  # name -> (x264 crf, x265 crf, gpu cq, videotoolbox q)
    "high": (18, 20, 19, 75),
    "balanced": (21, 24, 23, 65),
    "small": (25, 28, 27, 55),
}


# ---------------------------------------------------------------- job context (progress + cancel)
class Cancelled(Exception):
    pass


_ctx = threading.local()


@contextmanager
def job_context(progress=None, cancel: threading.Event | None = None):
    """progress(fraction 0..1, text) is called from the worker thread; set `cancel` to stop."""
    _ctx.progress, _ctx.cancel = progress, cancel or threading.Event()
    _ctx.batch = (0, 1)
    _ctx.span = (0.0, 1.0)
    try:
        yield _ctx.cancel
    finally:
        _ctx.progress = _ctx.cancel = None


def _report(frac: float, eta: float | None = None):
    cb = getattr(_ctx, "progress", None)
    if not cb:
        return
    i, n = getattr(_ctx, "batch", (0, 1))
    a, b = getattr(_ctx, "span", (0.0, 1.0))
    frac = a + (b - a) * max(0.0, min(1.0, frac))
    total = (i + frac) / max(1, n)
    bits = [f"{total * 100:.0f}%"]
    if n > 1:
        bits.insert(0, f"{i + 1}/{n}")
    if eta is not None and eta > 1:
        m, s = divmod(int(eta), 60)
        bits.append(f"~{m}:{s:02d} left" if m < 60 else f"~{m // 60}h {m % 60:02d}m left")
    cb(total, " · ".join(bits))


def _cancelled() -> bool:
    ev = getattr(_ctx, "cancel", None)
    return bool(ev and ev.is_set())


def run_batch(paths: list, fn) -> list[Path]:
    """Run fn(path) -> [outputs] over several files, with 'n/m' progress."""
    outs: list[Path] = []
    for i, p in enumerate(paths):
        if _cancelled():
            raise Cancelled()
        _ctx.batch = (i, len(paths))
        outs.extend(fn(p))
    return outs


# ---------------------------------------------------------------- finding FFmpeg
_FFMPEG: str | None = None


def ffmpeg_exe() -> str:
    """Bundled copy first (installer), then FFmpeg on PATH, then the imageio-ffmpeg wheel."""
    global _FFMPEG
    if _FFMPEG and os.path.exists(_FFMPEG):
        return _FFMPEG
    name = "ffmpeg.exe" if IS_WIN else "ffmpeg"
    cands = []
    if os.environ.get("DRAGSHIFT_FFMPEG"):
        cands.append(Path(os.environ["DRAGSHIFT_FFMPEG"]))
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    cands += [base / "ffmpeg" / name, Path(__file__).resolve().parent / "bin" / name]
    if getattr(sys, "frozen", False):
        cands.append(Path(sys.executable).resolve().parent / "ffmpeg" / name)
    for c in cands:
        if c.is_file():
            _FFMPEG = str(c)
            return _FFMPEG
    on_path = shutil.which("ffmpeg")
    if on_path:
        _FFMPEG = on_path
        return _FFMPEG
    try:
        import imageio_ffmpeg  # type: ignore
        _FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
        return _FFMPEG
    except Exception:
        pass
    raise UserError("FFmpeg wasn't found. The installer includes it; when running from source, "
                    "install it with “pip install imageio-ffmpeg” or put ffmpeg on your PATH.")


def _popen(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen([ffmpeg_exe(), "-hide_banner", "-nostdin", *args],
                            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, **kw)


def _run_quick(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", *args], capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=timeout, creationflags=NO_WINDOW)


# ---------------------------------------------------------------- probing
@dataclass
class Info:
    duration: float | None = None
    vcodec: str | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0
    pix_fmt: str = ""
    rotation: int = 0
    acodec: str | None = None
    audio_streams: int = 0
    abits: int = 16
    achannels: str = ""
    sample_rate: int = 0
    cover: bool = False      # audio file with embedded album art
    bitrate_kbps: int = 0
    raw: str = field(default="", repr=False)

    @property
    def has_video(self) -> bool:
        return self.vcodec is not None

    @property
    def has_audio(self) -> bool:
        return self.acodec is not None

    @property
    def ten_bit(self) -> bool:
        return bool(re.search(r"(10|12)(le|be)$", self.pix_fmt))

    @property
    def display_size(self) -> tuple[int, int]:
        """Width × height as shown (phone videos store rotation separately)."""
        if abs(self.rotation) % 180 == 90:
            return self.height, self.width
        return self.width, self.height


_DUR = re.compile(r"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)")
_BR = re.compile(r"bitrate: (\d+) kb/s")


def probe(src: str | Path) -> Info:
    """Parse `ffmpeg -i` output (no ffprobe needed, so only one binary ships)."""
    p = _run_quick(["-i", str(src)], timeout=60)
    text = p.stderr.decode("utf-8", "replace")
    info = Info(raw=text)
    if "Invalid data found" in text or "Stream #" not in text:
        raise UserError(_decode_failure(src, text))
    m = _DUR.search(text)
    if m:
        info.duration = int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3])
    m = _BR.search(text)
    if m:
        info.bitrate_kbps = int(m[1])
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("Stream #"):
            if "rotation of" in line and info.has_video and not info.rotation:
                r = re.search(r"rotation of (-?[\d.]+)", line)
                if r:
                    info.rotation = int(round(float(r[1])))
            continue
        if ": Video: " in line:
            if "(attached pic)" in line:
                info.cover = True
                continue
            if info.vcodec is not None:
                continue
            m = re.search(r": Video: (\w+)[^,]*?(?:, (\w+)(?:\([^)]*\))?)?, (\d{2,5})x(\d{2,5})", line)
            if m:
                info.vcodec = m[1]
                info.pix_fmt = m[2] or ""
                info.width, info.height = int(m[3]), int(m[4])
            else:
                info.vcodec = re.search(r": Video: (\w+)", line)[1]
            f = re.search(r"([\d.]+) fps", line) or re.search(r"([\d.]+) tbr", line)
            if f:
                info.fps = float(f[1])
        elif ": Audio: " in line:
            info.audio_streams += 1
            if info.acodec is None:
                info.acodec = re.search(r": Audio: (\w+)", line)[1]
                sr = re.search(r"(\d+) Hz", line)
                info.sample_rate = int(sr[1]) if sr else 0
                parts = line.split(", ")
                info.achannels = parts[2] if len(parts) > 2 else ""
                if re.search(r"\b(s24|s32|flt|dbl)\b|\(24 bit\)|\(32 bit\)", line) and \
                        (info.acodec.startswith("pcm") or info.acodec in ("flac", "alac", "wavpack", "truehd", "mlp")):
                    info.abits = 24
    return info


def _decode_failure(src, text: str) -> str:
    ext = Path(src).suffix.lower()
    if ext in UNDECODABLE_RAW:
        return UNDECODABLE_RAW[ext]
    if "prores_raw" in text.lower() or "aprh" in text or "aprn" in text:
        return "This is ProRes RAW, which needs FFmpeg 8 or newer. Update DragShift (it ships FFmpeg 8)."
    if "No such file" in text:
        return "The file was moved or deleted."
    return "FFmpeg couldn't read this file. It may be damaged, incomplete, or use an unsupported codec."


# ---------------------------------------------------------------- hardware encoders
_HW_CACHE: dict[str, str | None] = {}
_HW_LOCK = threading.Lock()


def _hw_candidates(codec: str) -> list[str]:
    if IS_MAC:
        return [f"{codec}_videotoolbox"]
    if IS_WIN:
        return [f"{codec}_nvenc", f"{codec}_qsv", f"{codec}_amf"]
    return [f"{codec}_nvenc"]


def hw_encoder(codec: str) -> str | None:
    """First GPU encoder that really works on this machine (tested once with a tiny clip)."""
    if not CONFIG.get("gpu_encode", True):
        return None
    with _HW_LOCK:
        if codec in _HW_CACHE:
            return _HW_CACHE[codec]
        found = None
        try:
            listing = _run_quick(["-encoders"], timeout=20).stdout.decode("utf-8", "replace")
            for enc in _hw_candidates(codec):
                if f" {enc} " not in listing:
                    continue
                pix = "nv12" if enc.endswith(("_qsv", "_amf")) else "yuv420p"
                t = _run_quick(["-f", "lavfi", "-i", "color=c=black:s=640x360:d=0.2", "-frames:v", "3",
                                "-pix_fmt", pix, "-c:v", enc, "-f", "null", "-"], timeout=20)
                if t.returncode == 0:
                    found = enc
                    break
        except Exception:
            found = None
        _HW_CACHE[codec] = found
        return found


def _video_args(codec: str, info: Info, quality: str, use_hw: bool = True) -> tuple[list[str], bool]:
    """Encoder arguments for H.264 ('h264') or H.265 ('hevc'). Returns (args, used_gpu)."""
    crf264, crf265, cq, vtq = QUALITY.get(quality, QUALITY["balanced"])
    ten = info.ten_bit and codec == "hevc"  # H.264 stays 8-bit 4:2:0 so it plays everywhere
    tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
    enc = hw_encoder(codec) if use_hw else None
    if enc:
        if enc.endswith("_nvenc"):
            q = cq + (2 if codec == "hevc" else 0)
            return (["-c:v", enc, "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(q), "-b:v", "0",
                     "-pix_fmt", "p010le" if ten else "yuv420p"] + tag, True)
        if enc.endswith("_qsv"):
            return (["-c:v", enc, "-preset", "slower", "-global_quality", str(cq),
                     "-pix_fmt", "p010le" if ten else "nv12"] + tag, True)
        if enc.endswith("_amf"):
            return (["-c:v", enc, "-quality", "quality", "-rc", "cqp", "-qp_i", str(cq), "-qp_p", str(cq + 2),
                     "-pix_fmt", "p010le" if ten else "nv12"] + tag, True)
        if enc.endswith("_videotoolbox"):
            return (["-c:v", enc, "-q:v", str(vtq), "-pix_fmt", "p010le" if ten else "yuv420p"] + tag, True)
    if codec == "hevc":
        return (["-c:v", "libx265", "-preset", "medium", "-crf", str(crf265), "-x265-params", "log-level=error",
                 "-pix_fmt", "yuv420p10le" if ten else "yuv420p"] + tag, False)
    return (["-c:v", "libx264", "-preset", "medium", "-crf", str(crf264), "-pix_fmt", "yuv420p",
             "-profile:v", "high"], False)


def _aac_args(info: Info, kbps: int = 256, allow_copy: bool = True) -> list[str]:
    if not info.has_audio:
        return []
    if allow_copy and info.acodec == "aac":
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", f"{kbps}k"]


# ---------------------------------------------------------------- running FFmpeg
def _tmp_for(out: Path) -> Path:
    return out.with_name(f".~{out.stem}.part{out.suffix}")


def run_ffmpeg(args: list[str], out: Path | None, duration: float | None) -> Path | None:
    """Run FFmpeg writing to a hidden temp file, report progress, rename when done.
    out=None runs an analysis pass that writes nothing (e.g. pass 1 of a 2-pass encode)."""
    if _cancelled():
        raise Cancelled()
    tmp = _tmp_for(out) if out is not None else None
    log = tempfile.TemporaryFile()
    dst = [str(tmp)] if tmp is not None else ["-f", "null", "-"]
    proc = _popen(["-y", "-loglevel", "error", "-progress", "pipe:1", "-nostats", *args, *dst],
                  stdout=subprocess.PIPE, stderr=log)
    t0 = time.monotonic()
    last = 0.0
    try:
        for raw in proc.stdout:  # type: ignore[union-attr]
            if _cancelled():
                proc.kill()
                break
            line = raw.decode("ascii", "replace").strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if duration and duration > 0:
                    frac = min(1.0, us / 1e6 / duration)
                    now = time.monotonic()
                    if now - last > 0.25:
                        el = now - t0
                        eta = el / frac * (1 - frac) if frac > 0.02 and el > 2 else None
                        _report(frac, eta)
                        last = now
        proc.wait()
        if _cancelled():
            raise Cancelled()
        if proc.returncode != 0 or (tmp is not None and (not tmp.exists() or tmp.stat().st_size == 0)):
            log.seek(0)
            err = log.read().decode("utf-8", "replace").strip().splitlines()
            raise FFmpegError(err[-1] if err else f"FFmpeg exited with code {proc.returncode}")
        if tmp is None:
            return None
        os.replace(tmp, out)
        _report(1.0)
        return out
    finally:
        if proc.poll() is None:
            proc.kill()
        log.close()
        if tmp is not None and tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


class FFmpegError(Exception):
    pass


def _encode(args_fn, out: Path, info: Info) -> Path:
    """args_fn(use_hw) -> (args, used_gpu). Falls back to the CPU encoder if the GPU one fails."""
    args, gpu = args_fn(True)
    try:
        return run_ffmpeg(args, out, info.duration)
    except FFmpegError:
        if not gpu:
            raise
        args, _ = args_fn(False)
        return run_ffmpeg(args, out, info.duration)


def _friendly(e: FFmpegError, src) -> UserError:
    msg = str(e)
    low = msg.lower()
    if "no space left" in low:
        return UserError("The disk is full.")
    if "permission denied" in low:
        return UserError("No permission to write next to this file. Try Settings → Save results → subfolder.")
    return UserError(f"FFmpeg: {msg}")


def _guard(fn):
    def wrapped(src, *a, **kw):
        ext = Path(src).suffix.lower()
        if ext in UNDECODABLE_RAW:
            raise UserError(UNDECODABLE_RAW[ext])
        try:
            return fn(src, *a, **kw)
        except FFmpegError as e:
            raise _friendly(e, src) from None
    wrapped.__name__ = fn.__name__
    return wrapped


# ---------------------------------------------------------------- video conversion
def _quality() -> str:
    return str(CONFIG.get("video_quality", "balanced"))


def _common_out_flags(container: str) -> list[str]:
    flags = ["-map_metadata", "0"]
    if container in (".mp4", ".mov", ".m4a"):
        flags += ["-movflags", "+faststart+use_metadata_tags"]
    return flags


@_guard
def video_to(src: str | Path, target: str) -> list[Path]:
    target = target.upper()
    info = probe(src)
    if target in ("MP3", "WAV"):
        if not info.has_audio:
            raise UserError("This video has no audio track.")
        return audio_to(src, target, info=info)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    ext = TARGET_EXT[target]
    out = sibling(src, TARGET_LABEL.get(target, ""), ext)
    s = ["-i", str(src)]
    maps = ["-map", "0:v:0", "-map", "0:a?"]

    if target in ("MP4", "HEVC"):
        codec = "h264" if target == "MP4" else "hevc"
        if info.vcodec == codec and info.acodec in (None, "aac", "mp3", "ac3", "eac3", "alac"):
            # already the right codec (e.g. iPhone/camera MOV): just rewrap, instant and lossless
            try:
                tag = ["-tag:v", "hvc1"] if codec == "hevc" else []
                return [run_ffmpeg(s + maps + ["-c", "copy"] + tag + _common_out_flags(ext), out, info.duration)]
            except FFmpegError:
                pass
        return [_encode(lambda hw: (lambda a: (s + maps + a[0] + _aac_args(info) + _common_out_flags(ext), a[1]))(
            _video_args(codec, info, _quality(), hw)), out, info)]

    if target == "PRORES":
        profile = "3" if _quality() == "high" else "2"   # 422 HQ or 422
        pcm = "pcm_s24le" if info.abits > 16 else "pcm_s16le"
        a = ["-c:a", pcm] if info.has_audio else []
        return [run_ffmpeg(s + maps + ["-c:v", "prores_ks", "-profile:v", profile, "-vendor", "apl0",
                                       "-pix_fmt", "yuv422p10le"] + a + _common_out_flags(ext), out, info.duration)]

    if target in ("MOV", "MKV"):
        extra = ["-map", "0:s?"] if target == "MKV" else []
        try:  # rewrap without re-encoding
            return [run_ffmpeg(s + maps + extra + ["-c", "copy"] + _common_out_flags(ext), out, info.duration)]
        except FFmpegError:
            pass
        try:  # video fits, audio doesn't (e.g. Opus into MOV)
            return [run_ffmpeg(s + maps + ["-c:v", "copy"] + _aac_args(info, allow_copy=False)
                               + _common_out_flags(ext), out, info.duration)]
        except FFmpegError:
            pass
        return [_encode(lambda hw: (lambda a: (s + maps + a[0] + _aac_args(info) + _common_out_flags(ext), a[1]))(
            _video_args("h264", info, _quality(), hw)), out, info)]

    if target == "WEBM":
        crf = {"high": 28, "balanced": 32, "small": 37}.get(_quality(), 32)
        a = ["-c:a", "libopus", "-b:a", "160k"] if info.has_audio else []
        return [run_ffmpeg(s + ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "libvpx-vp9", "-crf", str(crf),
                                "-b:v", "0", "-row-mt", "1", "-deadline", "good", "-cpu-used", "3",
                                "-pix_fmt", "yuv420p"] + a, out, info.duration)]

    if target == "GIF":
        limit = 30.0
        dur = min(info.duration or limit, limit)
        vf = ("fps=15,scale='min(640,iw)':-2:flags=lanczos,split[a][b];"
              "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4")
        return [run_ffmpeg(["-t", f"{dur:.2f}"] + s + ["-map", "0:v:0", "-vf", vf, "-loop", "0"],
                           out, dur)]

    raise ValueError(f"Can't convert video to {target}")


# ---------------------------------------------------------------- video tools
def _h264_tool(src, label: str, info: Info, vf: str | None = None, quality: str | None = None,
               audio: list[str] | None = None) -> Path:
    out = sibling(src, label, ".mp4")
    s = ["-i", str(src), "-map", "0:v:0", "-map", "0:a?"]
    f = ["-vf", vf] if vf else []
    a = audio if audio is not None else _aac_args(info)
    return _encode(lambda hw: (lambda v: (s + f + v[0] + a + _common_out_flags(".mp4"), v[1]))(
        _video_args("h264", info, quality or _quality(), hw)), out, info)


@_guard
def compress_video(src: str | Path) -> list[Path]:
    info = probe(src)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    return [_h264_tool(src, "Compressed", info, quality="small",
                       audio=(["-c:a", "aac", "-b:a", "128k"] if info.has_audio else []))]


@_guard
def scale_video(src: str | Path, short_side: int) -> list[Path]:
    info = probe(src)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    w, h = info.display_size
    if min(w, h) <= short_side:
        raise UserError(f"This video is already {w}×{h}, no bigger than {short_side}p.")
    vf = f"scale=-2:{short_side}:flags=lanczos" if w >= h else f"scale={short_side}:-2:flags=lanczos"
    return [_h264_tool(src, f"{short_side}p", info, vf=vf)]


@_guard
def rotate_video(src: str | Path) -> list[Path]:
    info = probe(src)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    return [_h264_tool(src, "Rotated", info, vf="transpose=clock")]


@_guard
def mute_video(src: str | Path) -> list[Path]:
    info = probe(src)
    ext = Path(src).suffix.lower()
    out = sibling(src, "Muted", ext)
    try:
        return [run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-c", "copy", "-an"] + _common_out_flags(ext),
                           out, info.duration)]
    except FFmpegError:
        return [_h264_tool(src, "Muted", info, audio=["-an"])]


@_guard
def thumbnail(src: str | Path) -> list[Path]:
    info = probe(src)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    at = (info.duration or 0) * 0.33
    out = sibling(src, "Thumbnail", ".jpg")
    return [run_ffmpeg(["-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1", "-q:v", "2",
                        "-vf", "scale=iw*sar:ih,setsar=1", "-update", "1"], out, None)]


def plan_fit(info: Info, target_bytes: int) -> tuple[int, int, int | None]:
    """(video kbps, audio kbps, short side to scale to or None) to land just under target_bytes."""
    dur = info.duration or 0
    if dur <= 0:
        raise UserError("Couldn't read the length of this video.")
    total_k = target_bytes * 8 / 1000 * 0.96 / dur      # 4 % for the container
    audio_k = 0
    if info.has_audio:
        audio_k = 128 if total_k > 1200 else 96 if total_k > 500 else 64
    video_k = int(total_k - audio_k)
    if video_k < 80:
        mb = (80 + audio_k) * dur / 8 / 1000 / 0.96 / 1000
        raise UserError(f"That's too small for {fmt_duration(dur)} of video. Try at least {mb:.1f} MB.")
    w, h = info.display_size
    short = min(w, h) if w and h else 0
    scale = None
    for limit, side in ((700, 480), (1500, 720), (4500, 1080)):
        if video_k < limit and short > side:
            scale = side
            break
    return video_k, audio_k, scale


@_guard
def fit_size(src: str | Path, target_bytes: int) -> list[Path]:
    """Two-pass H.264 so the MP4 ends up just under target_bytes (e.g. 25 MB for e-mail)."""
    info = probe(src)
    if not info.has_video:
        raise UserError("No video track found in this file.")
    video_k, audio_k, scale = plan_fit(info, target_bytes)
    mb = target_bytes / 1_000_000
    label = f"{mb:g} MB"
    out = sibling(src, label, ".mp4")
    w, h = info.display_size
    vf = []
    if scale:
        vf = ["-vf", f"scale=-2:{scale}:flags=lanczos" if w >= h else f"scale={scale}:-2:flags=lanczos"]
    v = ["-c:v", "libx264", "-preset", "medium", "-b:v", f"{video_k}k", "-pix_fmt", "yuv420p", "-profile:v", "high"]
    with tempfile.TemporaryDirectory() as d:
        log = str(Path(d) / "pass")
        s = ["-i", str(src), "-map", "0:v:0"]
        _ctx.span = (0.0, 0.45)
        try:
            run_ffmpeg(s + vf + v + ["-pass", "1", "-passlogfile", log, "-an"], None, info.duration)
            _ctx.span = (0.45, 1.0)
            a = ["-map", "0:a:0?", "-c:a", "aac", "-b:a", f"{audio_k}k"] if info.has_audio else []
            res = run_ffmpeg(s + a + vf + v + ["-pass", "2", "-passlogfile", log] + _common_out_flags(".mp4"),
                             out, info.duration)
        finally:
            _ctx.span = (0.0, 1.0)
    return [res]


# ---------------------------------------------------------------- audio
def _audio_codec(target: str, info: Info) -> list[str]:
    t = target.upper()
    deep = info.abits > 16
    if t == "MP3":
        return ["-c:a", "libmp3lame", "-b:a", "320k", "-id3v2_version", "3"]
    if t == "WAV":
        return ["-c:a", "pcm_s24le" if deep else "pcm_s16le"]
    if t == "AIFF":
        return ["-c:a", "pcm_s24be" if deep else "pcm_s16be"]
    if t == "FLAC":
        return ["-c:a", "flac", "-compression_level", "8"] + (["-sample_fmt", "s32"] if deep else ["-sample_fmt", "s16"])
    if t == "M4A":
        return ["-c:a", "aac", "-b:a", "256k"]
    if t == "ALAC":
        return ["-c:a", "alac"] + (["-sample_fmt", "s32p"] if deep else ["-sample_fmt", "s16p"])
    if t == "OGG":
        return ["-c:a", "libvorbis", "-q:a", "6"]
    if t == "OPUS":
        return ["-c:a", "libopus", "-b:a", "160k"]
    raise ValueError(f"Unknown audio target {target}")


def _audio_maps(target: str, info: Info, from_video: bool) -> list[str]:
    maps = ["-map", "0:a:0"]
    # keep album art where the container supports it
    if info.cover and not from_video and target.upper() in ("MP3", "FLAC", "M4A", "ALAC"):
        maps += ["-map", "0:v", "-c:v", "copy", "-disposition:v", "attached_pic"]
    else:
        maps += ["-vn"]
    return maps


@_guard
def audio_to(src: str | Path, target: str, info: Info | None = None, label: str = "",
             filters: str | None = None) -> list[Path]:
    target = target.upper()
    info = info or probe(src)
    if not info.has_audio:
        raise UserError("No audio track found in this file.")
    out = sibling(src, label or TARGET_LABEL.get(target, ""), TARGET_EXT[target])
    af = ["-af", filters] if filters else []
    args = ["-i", str(src)] + _audio_maps(target, info, info.has_video) + af + _audio_codec(target, info)
    if target == "OPUS" and info.sample_rate not in (8000, 12000, 16000, 24000, 48000):
        args += ["-ar", "48000"]
    args += _common_out_flags(TARGET_EXT[target])
    return [run_ffmpeg(args, out, info.duration)]


def _same_format_target(src) -> str:
    ext = Path(src).suffix.lower()
    return {".mp3": "MP3", ".wav": "WAV", ".flac": "FLAC", ".m4a": "M4A", ".aac": "M4A", ".ogg": "OGG",
            ".oga": "OGG", ".opus": "OPUS", ".aif": "AIFF", ".aiff": "AIFF", ".aifc": "AIFF"}.get(ext, "WAV")


@_guard
def normalize_audio(src: str | Path, target_lufs: float = -14.0) -> list[Path]:
    """Two-pass EBU R128 loudness normalisation (streaming level, true peak −1 dBTP)."""
    info = probe(src)
    if not info.has_audio:
        raise UserError("No audio track found in this file.")
    base = f"loudnorm=I={target_lufs}:TP=-1.0:LRA=11"
    _report(0.02)
    p = _run_quick(["-i", str(src), "-map", "0:a:0", "-af", base + ":print_format=json", "-f", "null", "-"],
                   timeout=3600)
    txt = p.stderr.decode("utf-8", "replace")
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", txt, re.S)
    if not m:
        raise UserError("Couldn't measure the loudness of this file.")
    meas = json.loads(m[0])
    if meas.get("input_i") in ("-inf", None):
        raise UserError("This audio is silent.")
    f = (f"{base}:measured_I={meas['input_i']}:measured_TP={meas['input_tp']}:measured_LRA={meas['input_lra']}"
         f":measured_thresh={meas['input_thresh']}:offset={meas['target_offset']}:linear=true")
    sr = info.sample_rate or 48000
    if info.has_video:
        ext = Path(src).suffix.lower()
        out = sibling(src, "Normalized", ext)
        return [run_ffmpeg(["-i", str(src), "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy",
                            "-af", f"{f},aresample={sr}", "-c:a", "aac", "-b:a", "256k"] + _common_out_flags(ext),
                           out, info.duration)]
    return audio_to(src, _same_format_target(src), info=info, label="Normalized", filters=f"{f},aresample={sr}")


@_guard
def trim_silence(src: str | Path) -> list[Path]:
    info = probe(src)
    f = ("silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,areverse,"
         "silenceremove=start_periods=1:start_threshold=-50dB:start_silence=0.1,areverse")
    return audio_to(src, _same_format_target(src), info=info, label="Trimmed", filters=f)


@_guard
def to_mono(src: str | Path) -> list[Path]:
    info = probe(src)
    return audio_to(src, _same_format_target(src), info=info, label="Mono", filters="pan=mono|c0=0.5*c0+0.5*c1"
                    if "stereo" in info.achannels else "aformat=channel_layouts=mono")


# ---------------------------------------------------------------- dispatch
def targets_for(cat: str, src_ext: str) -> list[str]:
    ext = src_ext.lower()
    if cat == "video":
        same = {".mov": "MOV", ".mkv": "MKV", ".webm": "WEBM", ".gif": "GIF"}.get(ext)
        return [t for t in VIDEO_TARGETS if t != same]
    if cat == "audio":
        same = _same_format_target("x" + ext) if ext in (".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga",
                                                          ".opus", ".aif", ".aiff", ".aifc") else None
        return [t for t in AUDIO_TARGETS if t != same]
    return []


def convert(src: str | Path, cat: str, target: str) -> list[Path]:
    if cat == "video":
        return video_to(src, target)
    return audio_to(src, target)


def tool(src: str | Path, name: str) -> list[Path]:
    fns = {"Compress": compress_video, "Fit size": lambda p: fit_size(p, 25_000_000), "1080p": lambda p: scale_video(p, 1080),
           "720p": lambda p: scale_video(p, 720), "Rotate": rotate_video, "Mute": mute_video,
           "Thumbnail": thumbnail, "Normalize": normalize_audio, "Trim silence": trim_silence, "Mono": to_mono}
    return fns[name](src)


def preview_frame(src: str | Path) -> Path | None:
    """Small JPG of a video frame for the notification thumbnail (temp file)."""
    try:
        info = probe(src)
        if not info.has_video:
            return None
        at = min(1.0, (info.duration or 0) * 0.1)
        out = Path(tempfile.gettempdir()) / f"dragshift-thumb-{os.getpid()}-{abs(hash(str(src)))}.jpg"
        r = _run_quick(["-y", "-ss", f"{at:.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale=160:-2", "-q:v", "4", "-update", "1", str(out)], timeout=30)
        return out if r.returncode == 0 and out.exists() else None
    except Exception:
        return None


def fmt_duration(sec: float | None) -> str:
    if not sec:
        return ""
    sec = int(round(sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
