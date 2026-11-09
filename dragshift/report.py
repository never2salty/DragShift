"""Turns 'what just happened' into a short, useful summary for the notification:
sizes saved, new dimensions, pages, fields removed…"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .util import category, file_size, human_size


@dataclass
class Result:
    title: str
    detail: str
    outs: list[Path]
    srcs: list[Path] = field(default_factory=list)
    good: str = ""     # highlighted figure, e.g. "−75%"
    preview: Path | None = None  # frame grabbed from a video result

    @property
    def thumb(self) -> Path | None:
        if self.preview is not None:
            return self.preview
        for o in self.outs:
            if category(o) == "image":
                return o
        return None


def dims(path: Path) -> tuple[int, int] | None:
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None


def pages(path: Path) -> int | None:
    try:
        from .convert import fitz
        with fitz.open(path) as d:
            return d.page_count
    except Exception:
        return None


def size_change(before: int, after: int) -> tuple[str, str]:
    """('2.4 MB → 610 KB', '75% smaller') — second part empty if no meaningful change."""
    text = f"{human_size(before)} → {human_size(after)}"
    if before <= 0:
        return text, ""
    pct = (1 - after / before) * 100
    if pct >= 1:
        return text, f"{pct:.0f}% smaller"
    if pct <= -1:
        return text, f"{-pct:.0f}% larger"
    return text, "same size"


def _dim_str(d) -> str:
    return f"{d[0]}×{d[1]}" if d else ""


def summarize(action: str, srcs: list, outs: list, extra: str = "") -> Result:
    srcs = [Path(s) for s in srcs]
    outs = [Path(o) for o in outs if o]
    if not outs:
        return Result(action, extra or "Nothing was written", [], srcs)
    src = srcs[0] if srcs else outs[0]
    before = sum(file_size(s) for s in srcs if s.is_file())
    after = sum(file_size(o) for o in outs if o.is_file())
    kind, _, name = action.partition(":")
    o0 = outs[0]
    good = ""
    if kind == "bulk":
        folders = {o.parent for o in outs}
        where = next(iter(folders)).name if len(folders) == 1 else f"{len(folders)} folders"
        detail = f"{len(outs)} files · {human_size(after)} · in {where}" + (f" · {extra}" if extra else "")
        return Result(f"Converted {len(srcs)} files", detail, outs, srcs,
                      good="" if extra else "all done")
    if category(src) in ("model", "dxf", "premiere", "ai"):
        return _other_summary(kind, name, srcs, outs, after)
    if category(src) in ("video", "audio"):
        return _media_summary(kind, name, srcs, outs, before, after)

    if kind == "convert":
        fmt = name
        if len(srcs) > 1 and len(outs) == 1:
            title = f"{len(srcs)} files → 1 {fmt}"
        elif len(outs) > 1 and len(srcs) == 1:
            n = pages(src)
            title = f"{n or len(outs)} pages → {len(outs)} {fmt}s"
        elif len(outs) > 1:
            title = f"{len(outs)} files → {fmt}"
        else:
            title = f"{src.suffix.lstrip('.').upper()} → {fmt}"
        bits = [o0.name if len(outs) == 1 else f"{o0.parent.name}/{o0.name} …"]
        d = dims(o0) if category(o0) == "image" else None
        if d:
            bits.append(_dim_str(d))
        if o0.suffix.lower() == ".pdf":
            n = pages(o0)
            if n:
                bits.append(f"{n} page{'s' if n != 1 else ''}")
        bits.append(human_size(after))
        return Result(title, " · ".join(bits), outs, srcs)

    if name == "Compress":
        sizes, verdict = size_change(before, after)
        title = f"Compressed {len(srcs)} files" if len(srcs) > 1 else "Compressed"
        if verdict.endswith("larger") or verdict == "same size":
            verdict = "already well compressed"
        return Result(title, sizes + (f" · {extra}" if extra else ""), outs, srcs, good=verdict)

    if name in ("Crop", "Resize", "Edit"):
        d0, d1 = dims(src), dims(o0)
        verb = {"Crop": "Cropped", "Resize": "Resized", "Edit": "Edited"}[name]
        if len(outs) > 1:
            return Result(f"{verb} {len(outs)} images", f"{human_size(before)} → {human_size(after)}", outs, srcs)
        detail = f"{_dim_str(d0)} → {_dim_str(d1)}" if d0 and d1 and d0 != d1 else _dim_str(d1)
        if extra:
            detail += f" · {extra}"
        return Result(verb, f"{detail} · {o0.name}", outs, srcs)

    if name == "Add BG":
        return Result("Background added", f"{_dim_str(dims(o0))} PNG · {human_size(after)} · {o0.name}", outs, srcs)

    if name == "Annotate":
        return Result("Annotated", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs)

    if name == "Redact":
        return Result("Redacted", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs,
                      good="burned into pixels")

    if name == "Metadata":
        return Result("Metadata removed", f"{extra} · {o0.name}" if extra else o0.name, outs, srcs,
                      good="private copy")

    if name == "Split":
        return Result(f"Split into {len(outs)} PDFs", f"{o0.name} …", outs, srcs)

    if name == "Merge":
        n = pages(o0)
        return Result(f"Merged {len(srcs)} PDFs", f"{n} pages · {human_size(after)} · {o0.name}", outs, srcs)

    if name == "Rotate":
        n = pages(o0)
        return Result("Rotated 90°", f"{n} page{'s' if n != 1 else ''} · {o0.name}", outs, srcs)

    if name == "Protect":
        return Result("Password protected", f"AES-256 · {o0.name}", outs, srcs, good="encrypted")

    if name == "Extract":
        return Result(f"Extracted {len(outs)} image{'s' if len(outs) != 1 else ''}",
                      f"{o0.parent.name}/ · {human_size(after)}", outs, srcs)

    # generic
    return Result(name or action, f"{o0.name} · {human_size(after)}", outs, srcs)


# ---------------------------------------------------------------- video & audio
def _media_summary(kind: str, name: str, srcs: list[Path], outs: list[Path], before: int, after: int) -> Result:
    from . import media
    o0 = outs[0]
    bits = [o0.name if len(outs) == 1 else f"{len(outs)} files"]
    info = None
    try:
        info = media.probe(o0)
    except Exception:
        pass
    if info is not None and len(outs) == 1:
        if info.has_video and info.width:
            w, h = info.display_size
            bits.append(f"{w}×{h}")
        if info.duration and o0.suffix.lower() not in (".jpg", ".png"):
            bits.append(media.fmt_duration(info.duration))
    bits.append(human_size(after))
    sizes, verdict = size_change(before, after)
    pct_smaller = verdict.endswith("smaller") and int(verdict.split("%")[0]) >= 10
    preview = media.preview_frame(o0) if info is not None and info.has_video and o0.suffix.lower() != ".gif" else None
    if kind == "convert":
        src_fmt = srcs[0].suffix.lstrip(".").upper()
        title = f"{len(srcs)} files → {name}" if len(srcs) > 1 else f"{src_fmt} → {name}"
        detail = " · ".join(bits)
        return Result(title, detail, outs, srcs, good=verdict if pct_smaller else "", preview=preview)
    if name == "Compress":
        if verdict.endswith("larger") or verdict == "same size":
            verdict = "already well compressed"
        title = f"Compressed {len(srcs)} videos" if len(srcs) > 1 else "Compressed"
        return Result(title, f"{sizes} · {o0.name}", outs, srcs, good=verdict, preview=preview)
    titles = {"1080p": "Scaled to 1080p", "720p": "Scaled to 720p", "Rotate": "Rotated 90°", "Mute": "Audio removed",
              "Thumbnail": "Frame saved", "Screenshots": "Screenshots saved", "Graded": "Graded video exported", "Normalize": "Loudness normalized", "Trim silence": "Silence trimmed",
              "Mono": "Mixed to mono"}
    if name == "Screenshots" and len(outs) == 1:
        titles["Screenshots"] = "Screenshot saved"
    good = {"Normalize": "−14 LUFS"}.get(name, "")
    if name in ("1080p", "720p") and pct_smaller:
        good = verdict
    return Result(titles.get(name, name), " · ".join(bits), outs, srcs, good=good, preview=preview)


def _other_summary(kind: str, name: str, srcs: list[Path], outs: list[Path], after: int) -> Result:
    o0 = outs[0]
    bits = [o0.name if len(outs) == 1 else f"{len(outs)} files"]
    if o0.suffix.lower() in (".stl", ".3mf", ".obj", ".ply", ".glb") and len(outs) == 1:
        from . import model3d
        d = model3d.describe(o0)
        if d:
            bits.append(d)
    bits.append(human_size(after))
    src_fmt = srcs[0].suffix.lstrip(".").upper()
    if kind == "convert":
        title = f"{len(srcs)} files → {name}" if len(srcs) > 1 else f"{src_fmt} → {name}"
    else:
        title = {"Repair": "Repaired", "Simplify": "Simplified", "Center": "Centered on the bed"}.get(name, name)
    good = "opens in older Premiere" if srcs[0].suffix.lower() == ".prproj" else ""
    return Result(title, " · ".join(bits), outs, srcs, good=good)
