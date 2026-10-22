"""Adobe files: Premiere Pro projects (open in an older Premiere) and Illustrator .ai files.

Premiere: a .prproj is gzip-compressed XML. Setting the <Project Version="…"> attribute to 1
lets an older Premiere open a project saved by a newer one (it then offers to convert it).
Effects that only exist in the newer version can be missing, so the original stays untouched.

Illustrator: .ai files saved with "Create PDF Compatible File" (Illustrator's default) contain
a full PDF, so they convert to PDF, PNG, JPG or SVG without Illustrator.
"""
from __future__ import annotations

import gzip
import re
from pathlib import Path

from .util import UserError, sibling

PREMIERE_TARGETS = ["Older Premiere"]
AI_TARGETS = ["PDF", "PNG", "JPG", "SVG"]

_VERSION = re.compile(rb'(<Project\b[^>]*?\bVersion=")(\d+)(")')


def premiere_version(src: str | Path) -> int | None:
    data = _read_prproj(src)
    m = _VERSION.search(data[:200_000])
    return int(m[2]) if m else None


def _read_prproj(src) -> bytes:
    raw = Path(src).read_bytes()
    if raw[:2] == b"\x1f\x8b":
        try:
            return gzip.decompress(raw)
        except OSError as e:
            raise UserError("This Premiere project is damaged (couldn't unpack it).") from e
    if raw.lstrip()[:5] == b"<?xml":
        return raw  # some tools save it uncompressed
    raise UserError("This doesn't look like a Premiere Pro project.")


def premiere_for_older(src: str | Path) -> list[Path]:
    """Copy of the project that older Premiere versions can open."""
    data = _read_prproj(src)
    m = _VERSION.search(data[:200_000])
    if not m:
        raise UserError("Couldn't find the project version inside this file.")
    if m[2] == b"1":
        raise UserError("This project is already marked to open in any Premiere version.")
    data = data[:m.start(2)] + b"1" + data[m.end(2):]
    out = sibling(src, "for older Premiere", ".prproj")
    tmp = out.with_name(f".~{out.name}.part")
    try:
        tmp.write_bytes(gzip.compress(data, compresslevel=6))
        tmp.replace(out)
    finally:
        if tmp.exists():
            tmp.unlink()
    return [out]


def ai_to(src: str | Path, target: str) -> list[Path]:
    from PIL import Image
    from .convert import fitz, save_image
    from .util import CONFIG
    target = target.upper()
    try:
        doc = fitz.open(str(src), filetype="pdf")
    except Exception:
        doc = None
    if doc is None or doc.page_count == 0:
        raise UserError("This .ai file was saved without PDF compatibility. In Illustrator: "
                        "File → Save As → enable “Create PDF Compatible File”.")
    with doc:
        if target == "PDF":
            out = sibling(src, ext=".pdf")
            doc.save(str(out), garbage=3, deflate=True)
            return [out]
        outs = []
        n = doc.page_count
        for i, page in enumerate(doc):
            label = f"artboard {i + 1}" if n > 1 else ""
            if target == "SVG":
                out = sibling(src, label, ".svg")
                out.write_text(page.get_svg_image(text_as_path=True), encoding="utf-8")
            else:
                dpi = int(CONFIG.get("pdf_dpi", 200))
                pix = page.get_pixmap(dpi=dpi, alpha=(target == "PNG"))
                mode = "RGBA" if pix.alpha else "RGB"
                im = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
                out = sibling(src, label, "." + target.lower())
                save_image(im, out, target)
            outs.append(out)
        return outs
