"""One place that knows which module converts which kind of file."""
from __future__ import annotations

from pathlib import Path

from . import adobe, convert, media, model3d
from .util import category


def targets_for(cat: str, ext: str) -> list[str]:
    if cat in ("video", "audio"):
        return media.targets_for(cat, ext)
    if cat in ("model", "dxf"):
        return model3d.targets_for(cat, ext)
    if cat == "premiere":
        return adobe.PREMIERE_TARGETS
    if cat == "ai":
        return adobe.AI_TARGETS
    return convert.targets_for(cat, ext)


def convert_file(src: str | Path, cat: str | None, target: str) -> list[Path]:
    cat = cat or category(src)
    if cat in ("video", "audio"):
        return media.convert(src, cat, target)
    if cat in ("model", "dxf"):
        return model3d.convert(src, cat, target)
    if cat == "premiere":
        return adobe.premiere_for_older(src)
    if cat == "ai":
        return adobe.ai_to(src, target)
    return convert.convert(src, cat, target)


# What a folder conversion picks by default for each extension (None = leave these files alone)
_KEEP = {".jpg", ".jpeg", ".mp4", ".mp3", ".pdf", ".stl", ".txt", ".md", ".csv", ".log", ".json", ".xml",
         ".html", ".htm", ".docx", ".prproj", ".dxf", ".gif", ".ico"}


def default_target(cat: str, ext: str) -> str | None:
    ext = ext.lower()
    if ext in _KEEP:
        return None
    return {"image": "JPG", "video": "MP4", "audio": "MP3", "model": "STL", "ai": "PDF"}.get(cat)
