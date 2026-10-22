"""Shared helpers: output paths, file categories, sizes."""
from __future__ import annotations

import os
from pathlib import Path

# Camera RAW photos (decoded with LibRaw through rawpy)
RAW_EXTS = {".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".dng", ".raf", ".orf",
            ".rw2", ".rwl", ".pef", ".srw", ".3fr", ".fff", ".iiq", ".erf", ".kdc", ".dcr", ".mrw",
            ".mos", ".x3f", ".gpr"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".webp", ".heic", ".heif", ".hif", ".tif", ".tiff",
              ".avif", ".bmp", ".gif", ".ico", ".psd", ".tga"} | RAW_EXTS
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".qt", ".mkv", ".webm", ".avi", ".wmv", ".asf", ".flv", ".f4v",
              ".mpg", ".mpeg", ".m2v", ".vob", ".mts", ".m2ts", ".mxf", ".3gp", ".3g2", ".ogv", ".dv",
              ".divx", ".rm", ".rmvb", ".y4m", ".insv", ".lrv",
              # camera RAW video: ProRes RAW (in .mov) decodes with FFmpeg 8+, the others need vendor SDKs
              ".braw", ".r3d", ".ari", ".arx", ".crm", ".nev"}
AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff",
              ".aifc", ".ape", ".wv", ".amr", ".ac3", ".eac3", ".dts", ".mka", ".m4b", ".caf",
              ".au", ".snd", ".mpc", ".tta", ".weba", ".mp2", ".w64", ".rf64", ".bwf"}
MODEL_EXTS = {".stl", ".3mf", ".obj", ".ply", ".glb", ".gltf", ".off", ".step", ".stp", ".scad"}
DXF_EXTS = {".dxf"}
PREMIERE_EXTS = {".prproj"}
AI_EXTS = {".ai"}            # Illustrator files saved with PDF compatibility (the default)
PDF_EXTS = {".pdf"}
DOCX_EXTS = {".docx"}
TEXT_EXTS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".html", ".htm"}


def category(path: str | Path) -> str | None:
    ext = Path(path).suffix.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext in MODEL_EXTS:
        return "model"
    if ext in DXF_EXTS:
        return "dxf"
    if ext in PREMIERE_EXTS:
        return "premiere"
    if ext in AI_EXTS:
        return "ai"
    if ext in VIDEO_EXTS:
        return "video"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in PDF_EXTS:
        return "pdf"
    if ext in DOCX_EXTS:
        return "docx"
    if ext in TEXT_EXTS:
        return "text"
    return None


def resolve_shortcuts(paths: list[str]) -> list[str]:
    """Windows .lnk shortcuts (desktop icons) stand for the file they point to."""
    if os.name != "nt" or not any(str(p).lower().endswith(".lnk") for p in paths):
        return list(paths)
    out = []
    for p in paths:
        if str(p).lower().endswith(".lnk"):
            try:
                import win32com.client  # type: ignore
                target = win32com.client.Dispatch("WScript.Shell").CreateShortcut(str(p)).TargetPath
                if target and os.path.isfile(target):
                    p = target
            except Exception:
                pass
        out.append(p)
    return out


def unique_path(path: str | Path) -> Path:
    """Return `path`, or `name (1).ext`, `name (2).ext`... if it already exists."""
    p = Path(path)
    if not p.exists():
        return p
    i = 1
    while True:
        cand = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not cand.exists():
            return cand
        i += 1


class UserError(Exception):
    """An error with a message that's fine to show the user as-is."""


# Runtime options (filled from Settings at start-up; plain values so worker threads can read them)
CONFIG = {
    "output": "next",        # "next" = beside the original, "folder" = a "DragShift" subfolder
    "jpg_quality": 90,
    "pdf_dpi": 200,
}


_OUT = __import__("threading").local()


class output_override:
    """with output_override(fn): results of this thread go to fn(src) (a folder) instead."""

    def __init__(self, fn):
        self.fn = fn

    def __enter__(self):
        self.prev = getattr(_OUT, "fn", None)
        _OUT.fn = self.fn
        return self

    def __exit__(self, *a):
        _OUT.fn = self.prev


def output_dir(src: str | Path) -> Path:
    fn = getattr(_OUT, "fn", None)
    if fn is not None:
        d = Path(fn(Path(src)))
        d.mkdir(parents=True, exist_ok=True)
        return d
    d = Path(src).parent
    if CONFIG.get("output") == "folder":
        d = d / "DragShift"
        d.mkdir(exist_ok=True)
    return d


def sibling(src: str | Path, suffix_label: str = "", ext: str | None = None) -> Path:
    """Output path for `src`. sibling('a.jpg', 'Cropped') -> 'a Cropped.jpg' (never overwrites)."""
    s = output_dir(src) / Path(src).name
    stem = f"{s.stem} {suffix_label}".strip() if suffix_label else s.stem
    new_ext = ext if ext is not None else s.suffix
    if not new_ext.startswith("."):
        new_ext = "." + new_ext
    return unique_path(s.with_name(stem + new_ext))


def human_size(n: int | float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def file_size(path: str | Path) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------- background job lifetime
# QThreadPool runs a QRunnable in C++, but the Python object (and the QObject carrying its
# signals) can be garbage-collected while it's still running: results then never arrive,
# or the app crashes. Keep every job alive until its result has been handled.
ALIVE: set = set()


def keep(job):
    job.setAutoDelete(False)
    ALIVE.add(job)
    return job


def release(job):
    ALIVE.discard(job)


# ---------------------------------------------------------------- fonts
_FONT_FAMILY: dict[bool, str] = {}
_DISPLAY = ["Segoe UI Variable Display", "Segoe UI", "SF Pro Display", "SF Pro", "Helvetica Neue",
            "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"]
_TEXT = ["Segoe UI Variable Text", "Segoe UI", "SF Pro Text", "SF Pro", "Helvetica Neue",
         "Inter", "Cantarell", "Noto Sans", "Ubuntu", "DejaVu Sans"]


def ui_family(display: bool = True) -> str:
    """First UI font that is really installed. Never ask Qt for a missing family: on macOS 26
    an unknown family can resolve to a font that draws nothing (labels vanish)."""
    if display not in _FONT_FAMILY:
        from PySide6.QtGui import QFontDatabase
        have = set(QFontDatabase.families())
        pick = next((f for f in (_DISPLAY if display else _TEXT) if f in have), None)
        if pick is None:
            pick = QFontDatabase.systemFont(QFontDatabase.GeneralFont).family()
        _FONT_FAMILY[display] = pick
    return _FONT_FAMILY[display]


def ui_font(px: int = 13, bold: bool = False, display: bool = True):
    from PySide6.QtGui import QFont
    f = QFont(ui_family(display))
    f.setPixelSize(int(px))
    f.setBold(bold)
    return f
