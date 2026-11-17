"""Folder icon library: colourful folders, symbols, your own pictures – applied by drag & drop.

Windows: an .ico inside the folder (hidden) + desktop.ini → Explorer shows it everywhere, also on
other PCs and external drives. macOS: Finder custom icon (NSWorkspace). Linux: gio metadata.
"""
from __future__ import annotations

import ctypes
import hashlib
import math
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QImage, QLinearGradient, QPainter, QPainterPath, QPen,
                           QPolygonF)

from .util import UserError, data_dir, ui_family

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
ICON_PREFIX = ".dragshift-icon-"
SIZE = 256

COLORS = [("Yellow", "#f7c948"), ("Orange", "#ff9f43"), ("Red", "#ff5d5d"), ("Pink", "#ff6fb5"),
          ("Purple", "#a970ff"), ("Indigo", "#6c7cff"), ("Blue", "#3ea6ff"), ("Teal", "#2ec4b6"),
          ("Green", "#4cd37b"), ("Lime", "#b5e04b"), ("Brown", "#b0835a"), ("Grey", "#8f97a3"),
          ("Black", "#3a3d45"), ("White", "#eef1f5")]
SYMBOLS = ["", "video", "music", "photo", "document", "code", "star", "heart", "check", "download",
           "cloud", "lock", "camera", "cube", "money", "user", "work", "archive"]
SYMBOL_NAMES = {"": "No symbol", "video": "Video", "music": "Music", "photo": "Photos", "document": "Documents",
                "code": "Code", "star": "Star", "heart": "Heart", "check": "Done", "download": "Downloads",
                "cloud": "Cloud", "lock": "Private", "camera": "Camera", "cube": "3D", "money": "Money",
                "user": "Person", "work": "Work", "archive": "Archive"}
# the built-in set: plain colours, then symbols in a colour that suits them
BUILTIN_SYMBOLS = [("video", "#ff5d5d"), ("music", "#a970ff"), ("photo", "#2ec4b6"), ("document", "#3ea6ff"),
                   ("code", "#3a3d45"), ("star", "#f7c948"), ("heart", "#ff6fb5"), ("check", "#4cd37b"),
                   ("download", "#6c7cff"), ("cloud", "#3ea6ff"), ("lock", "#8f97a3"), ("camera", "#ff9f43"),
                   ("cube", "#a970ff"), ("money", "#4cd37b"), ("user", "#ff9f43"), ("work", "#b0835a"),
                   ("archive", "#8f97a3")]


# ---------------------------------------------------------------- drawing
def _shade(c: QColor, f: float) -> QColor:
    h, s, v, a = c.getHsvF()
    return QColor.fromHsvF(max(h, 0), min(1, s * (1.05 if f < 1 else 0.9)), max(0, min(1, v * f)), a)


def _symbol_path(name: str, cx: float, cy: float, r: float) -> tuple[QPainterPath, bool]:
    """Vector symbol centred at (cx, cy) with radius r. Returns (path, stroke) – stroke=True: draw outline."""
    p = QPainterPath()
    if name == "video":
        p.addPolygon(QPolygonF([QPointF(cx - r * 0.55, cy - r * 0.75), QPointF(cx + r * 0.8, cy),
                                QPointF(cx - r * 0.55, cy + r * 0.75)]))
        p.closeSubpath()
    elif name == "music":
        p.addEllipse(QPointF(cx - r * 0.45, cy + r * 0.55), r * 0.36, r * 0.28)
        p.addEllipse(QPointF(cx + r * 0.55, cy + r * 0.35), r * 0.36, r * 0.28)
        p.addRect(QRectF(cx - r * 0.18, cy - r * 0.75, r * 0.15, r * 1.3))
        p.addRect(QRectF(cx + r * 0.82, cy - r * 0.95, r * 0.15, r * 1.3))
        poly = QPolygonF([QPointF(cx - r * 0.18, cy - r * 0.75), QPointF(cx + r * 0.97, cy - r * 0.95),
                          QPointF(cx + r * 0.97, cy - r * 0.65), QPointF(cx - r * 0.18, cy - r * 0.45)])
        p.addPolygon(poly)
        p.setFillRule(Qt.WindingFill)
    elif name == "photo":
        p.addPolygon(QPolygonF([QPointF(cx - r, cy + r * 0.7), QPointF(cx - r * 0.3, cy - r * 0.25),
                                QPointF(cx + r * 0.15, cy + r * 0.3), QPointF(cx + r * 0.45, cy),
                                QPointF(cx + r, cy + r * 0.7)]))
        p.addEllipse(QPointF(cx + r * 0.5, cy - r * 0.55), r * 0.22, r * 0.22)
        p.setFillRule(Qt.WindingFill)
    elif name == "document":
        for i, w in enumerate((1.0, 1.0, 0.75, 0.9)):
            p.addRoundedRect(QRectF(cx - r * 0.85, cy - r * 0.8 + i * r * 0.45, r * 1.7 * w, r * 0.2), r * 0.1, r * 0.1)
    elif name == "code":
        f = QFont(ui_family(False))
        f.setPixelSize(int(r * 1.5))
        f.setBold(True)
        p.addText(0, 0, f, "</>")
        b = p.boundingRect()
        p.translate(cx - b.center().x(), cy - b.center().y())
    elif name == "star":
        pts = []
        for i in range(10):
            rr = r if i % 2 == 0 else r * 0.45
            a = -math.pi / 2 + i * math.pi / 5
            pts.append(QPointF(cx + rr * math.cos(a), cy + rr * math.sin(a)))
        p.addPolygon(QPolygonF(pts))
        p.closeSubpath()
    elif name == "heart":
        p.moveTo(cx, cy + r * 0.85)
        p.cubicTo(cx - r * 1.3, cy - r * 0.05, cx - r * 0.65, cy - r * 1.05, cx, cy - r * 0.4)
        p.cubicTo(cx + r * 0.65, cy - r * 1.05, cx + r * 1.3, cy - r * 0.05, cx, cy + r * 0.85)
    elif name == "check":
        p.moveTo(cx - r * 0.75, cy)
        p.lineTo(cx - r * 0.2, cy + r * 0.55)
        p.lineTo(cx + r * 0.8, cy - r * 0.55)
        return p, True
    elif name == "download":
        p.addRect(QRectF(cx - r * 0.15, cy - r * 0.9, r * 0.3, r * 0.9))
        p.addPolygon(QPolygonF([QPointF(cx - r * 0.55, cy - r * 0.1), QPointF(cx + r * 0.55, cy - r * 0.1),
                                QPointF(cx, cy + r * 0.5)]))
        p.addRoundedRect(QRectF(cx - r * 0.85, cy + r * 0.62, r * 1.7, r * 0.22), r * 0.1, r * 0.1)
        p.setFillRule(Qt.WindingFill)
    elif name == "cloud":
        for (dx, dy, rr) in ((-0.45, 0.15, 0.42), (0.05, -0.15, 0.55), (0.5, 0.15, 0.4)):
            p.addEllipse(QPointF(cx + dx * r, cy + dy * r), rr * r, rr * r)
        p.addRoundedRect(QRectF(cx - r * 0.85, cy + r * 0.05, r * 1.75, r * 0.5), r * 0.25, r * 0.25)
        p.setFillRule(Qt.WindingFill)
        p = p.simplified()
    elif name == "lock":
        p.addRoundedRect(QRectF(cx - r * 0.7, cy - r * 0.1, r * 1.4, r * 0.95), r * 0.15, r * 0.15)
        arc = QPainterPath()
        arc.addRoundedRect(QRectF(cx - r * 0.45, cy - r * 0.85, r * 0.9, r * 1.2), r * 0.45, r * 0.45)
        inner = QPainterPath()
        inner.addRoundedRect(QRectF(cx - r * 0.25, cy - r * 0.65, r * 0.5, r * 0.9), r * 0.25, r * 0.25)
        p = p.united(arc.subtracted(inner))
    elif name == "camera":
        p.addRoundedRect(QRectF(cx - r * 0.95, cy - r * 0.5, r * 1.9, r * 1.25), r * 0.2, r * 0.2)
        p.addRoundedRect(QRectF(cx - r * 0.35, cy - r * 0.75, r * 0.7, r * 0.35), r * 0.1, r * 0.1)
        p.setFillRule(Qt.WindingFill)
        lens = QPainterPath()
        lens.addEllipse(QPointF(cx, cy + r * 0.12), r * 0.38, r * 0.38)
        p = p.simplified().subtracted(lens)
        dot = QPainterPath()
        dot.addEllipse(QPointF(cx, cy + r * 0.12), r * 0.2, r * 0.2)
        p = p.united(dot)
    elif name == "cube":
        top = QPolygonF([QPointF(cx, cy - r), QPointF(cx + r * 0.87, cy - r * 0.5), QPointF(cx, cy),
                         QPointF(cx - r * 0.87, cy - r * 0.5)])
        p.addPolygon(top)
        p.closeSubpath()
        p.addPolygon(QPolygonF([QPointF(cx - r * 0.87, cy - r * 0.38), QPointF(cx - r * 0.06, cy + r * 0.1),
                                QPointF(cx - r * 0.06, cy + r), QPointF(cx - r * 0.87, cy + r * 0.5)]))
        p.closeSubpath()
        p.addPolygon(QPolygonF([QPointF(cx + r * 0.87, cy - r * 0.38), QPointF(cx + r * 0.06, cy + r * 0.1),
                                QPointF(cx + r * 0.06, cy + r), QPointF(cx + r * 0.87, cy + r * 0.5)]))
        p.closeSubpath()
    elif name == "money":
        f = QFont(ui_family(False))
        f.setPixelSize(int(r * 2.0))
        f.setBold(True)
        p.addText(0, 0, f, "€")
        b = p.boundingRect()
        p.translate(cx - b.center().x(), cy - b.center().y())
    elif name == "user":
        p.addEllipse(QPointF(cx, cy - r * 0.45), r * 0.42, r * 0.42)
        body = QPainterPath()
        body.addRoundedRect(QRectF(cx - r * 0.85, cy + r * 0.1, r * 1.7, r * 1.2), r * 0.6, r * 0.6)
        clip = QPainterPath()
        clip.addRect(QRectF(cx - r, cy + r * 0.1, r * 2, r * 0.8))
        p = p.united(body.intersected(clip))
    elif name == "work":
        p.addRoundedRect(QRectF(cx - r * 0.95, cy - r * 0.45, r * 1.9, r * 1.25), r * 0.18, r * 0.18)
        handle = QPainterPath()
        handle.addRoundedRect(QRectF(cx - r * 0.4, cy - r * 0.8, r * 0.8, r * 0.5), r * 0.12, r * 0.12)
        hole = QPainterPath()
        hole.addRect(QRectF(cx - r * 0.25, cy - r * 0.65, r * 0.5, r * 0.25))
        p = p.united(handle.subtracted(hole))
    elif name == "archive":
        p.addRoundedRect(QRectF(cx - r * 0.95, cy - r * 0.8, r * 1.9, r * 0.45), r * 0.1, r * 0.1)
        p.addRoundedRect(QRectF(cx - r * 0.82, cy - r * 0.28, r * 1.64, r * 1.1), r * 0.1, r * 0.1)
        slot = QPainterPath()
        slot.addRoundedRect(QRectF(cx - r * 0.3, cy - r * 0.08, r * 0.6, r * 0.18), r * 0.09, r * 0.09)
        p.setFillRule(Qt.WindingFill)
        p = p.simplified().subtracted(slot)
    return p, False


def render_folder(color: str, symbol: str = "", text: str = "", size: int = SIZE) -> QImage:
    """A modern folder in `color`, optionally with a white symbol or 1–3 characters (emoji work too)."""
    img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.TextAntialiasing)
    s = size / 256.0
    base = QColor(color)
    # back with tab
    back = QPainterPath()
    back.moveTo(18 * s, 70 * s)
    back.quadTo(18 * s, 40 * s, 46 * s, 40 * s)
    back.lineTo(98 * s, 40 * s)
    back.quadTo(110 * s, 40 * s, 118 * s, 50 * s)
    back.lineTo(128 * s, 62 * s)
    back.lineTo(212 * s, 62 * s)
    back.quadTo(238 * s, 62 * s, 238 * s, 88 * s)
    back.lineTo(238 * s, 200 * s)
    back.lineTo(18 * s, 200 * s)
    back.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(_shade(base, 0.78))
    p.drawPath(back)
    # paper peeking out
    p.setBrush(QColor(255, 255, 255, 235))
    p.drawRoundedRect(QRectF(36 * s, 72 * s, 184 * s, 60 * s), 8 * s, 8 * s)
    # front
    front = QPainterPath()
    front.addRoundedRect(QRectF(14 * s, 92 * s, 228 * s, 132 * s), 22 * s, 22 * s)
    g = QLinearGradient(0, 92 * s, 0, 224 * s)
    g.setColorAt(0, _shade(base, 1.08))
    g.setColorAt(1, _shade(base, 0.9))
    p.setBrush(g)
    p.drawPath(front)
    p.setPen(QPen(QColor(255, 255, 255, 90), 2.2 * s))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(15 * s, 93 * s, 226 * s, 130 * s), 21 * s, 21 * s)
    # emblem
    lum = base.red() * 0.3 + base.green() * 0.59 + base.blue() * 0.11
    ink = QColor(40, 42, 50, 230) if lum > 200 else QColor(255, 255, 255, 240)
    cx, cy, r = 128 * s, 158 * s, 38 * s
    if text:
        f = QFont(ui_family(False))
        f.setBold(True)
        f.setPixelSize(int((70 if len(text) <= 1 else 54 if len(text) == 2 else 40) * s))
        p.setFont(f)
        p.setPen(ink)
        p.drawText(QRectF(14 * s, 92 * s, 228 * s, 132 * s), Qt.AlignCenter, text)
    elif symbol:
        path, stroke = _symbol_path(symbol, cx, cy, r)
        if stroke:
            p.setPen(QPen(ink, 16 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            p.setBrush(Qt.NoBrush)
        else:
            p.setPen(Qt.NoPen)
            p.setBrush(ink)
        p.drawPath(path)
    p.end()
    return img


# ---------------------------------------------------------------- the library
@dataclass
class IconEntry:
    name: str
    color: str = ""
    symbol: str = ""
    text: str = ""
    file: Path | None = None        # your own pictures (PNG in the library folder)

    @property
    def builtin(self) -> bool:
        return self.file is None

    def image(self, size: int = SIZE) -> QImage:
        if self.file is not None:
            img = QImage(str(self.file))
            if img.isNull():
                img = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
                img.fill(0)
            return img.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        return render_folder(self.color, self.symbol, self.text, size)

    def key(self) -> str:
        if self.file is not None:
            return f"file:{self.file.name}"
        return f"gen:{self.color}:{self.symbol}:{self.text}"


def library_dir() -> Path:
    d = data_dir() / "icons"
    d.mkdir(parents=True, exist_ok=True)
    return d


def builtin_icons() -> list[IconEntry]:
    out = [IconEntry(n, color=c) for n, c in COLORS]
    out += [IconEntry(SYMBOL_NAMES[s], color=c, symbol=s) for s, c in BUILTIN_SYMBOLS]
    return out


def custom_icons() -> list[IconEntry]:
    out = []
    for f in sorted(library_dir().glob("*.png"), key=lambda f: f.stat().st_mtime, reverse=True):
        out.append(IconEntry(f.stem, file=f))
    return out


def all_icons() -> list[IconEntry]:
    return custom_icons() + builtin_icons()


def _safe(name: str) -> str:
    return re.sub(r"[^\w\- ]+", "", name).strip()[:40] or "Icon"


def add_image(src: str | Path) -> IconEntry:
    """Put a picture (PNG, JPG, ICO, HEIC, WebP…) into the library as a 256×256 PNG."""
    from PIL import Image
    from .convert import open_image
    src = Path(src)
    try:
        if src.suffix.lower() == ".ico":
            im = Image.open(src)
            if hasattr(im, "ico"):
                im.size = max(im.ico.sizes(), key=lambda s: s[0] * s[1])   # the largest picture inside
            im = im.convert("RGBA")
        else:
            im = open_image(src).convert("RGBA")
    except Exception as e:  # noqa: BLE001
        raise UserError(f"Can't use {src.name} as an icon ({e.__class__.__name__}).") from None
    bbox = im.getbbox()
    if bbox:
        im = im.crop(bbox)
    side = max(im.size)
    sq = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    sq.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
    sq = sq.resize((SIZE, SIZE), Image.LANCZOS)
    dest = library_dir() / f"{_safe(src.stem)}.png"
    n = 2
    while dest.exists():
        dest = library_dir() / f"{_safe(src.stem)} {n}.png"
        n += 1
    sq.save(dest)
    return IconEntry(dest.stem, file=dest)


def save_custom(color: str, symbol: str, text: str, name: str = "") -> IconEntry:
    img = render_folder(color, symbol, text)
    nm = _safe(name or text or SYMBOL_NAMES.get(symbol, "") or "Folder")
    dest = library_dir() / f"{nm}.png"
    n = 2
    while dest.exists():
        dest = library_dir() / f"{nm} {n}.png"
        n += 1
    img.save(str(dest))
    return IconEntry(dest.stem, file=dest)


def remove_custom(e: IconEntry):
    if e.file is not None:
        e.file.unlink(missing_ok=True)


def _pil(img: QImage):
    from .glass import pil_from_qimage
    return pil_from_qimage(img.convertToFormat(QImage.Format_RGBA8888)).convert("RGBA")


def write_ico(img: QImage, dest: Path):
    _pil(img).save(dest, format="ICO", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48),
                                              (64, 64), (96, 96), (128, 128), (256, 256)])


# ---------------------------------------------------------------- desktop.ini (Windows)
def _read_ini(p: Path) -> tuple[list[str], str]:
    if not p.exists():
        return [], "utf-16"
    raw = p.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        return raw.decode("utf-16").splitlines(), "utf-16"
    try:
        return raw.decode("utf-8-sig").splitlines(), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("mbcs" if IS_WIN else "latin-1", "replace").splitlines(), "mbcs" if IS_WIN else "latin-1"


def ini_set_icon(lines: list[str], value: str | None) -> list[str]:
    """Set (or remove, value=None) IconResource in [.ShellClassInfo]; keeps everything else."""
    out, in_sec, found_sec, done = [], False, False, False
    drop = ("iconresource", "iconfile", "iconindex")
    for ln in lines:
        st = ln.strip()
        if st.startswith("[") and st.endswith("]"):
            if in_sec and value and not done:
                out.append(f"IconResource={value}")
                done = True
            in_sec = st.lower() == "[.shellclassinfo]"
            found_sec = found_sec or in_sec
            out.append(ln)
            continue
        if in_sec and "=" in st and st.split("=", 1)[0].strip().lower() in drop:
            continue
        out.append(ln)
    if value and not done:
        if in_sec:
            out.append(f"IconResource={value}")
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["[.ShellClassInfo]", f"IconResource={value}"]
    return out


def ini_is_empty(lines: list[str]) -> bool:
    """Nothing left but empty sections / comments."""
    return not any(ln.strip() and not ln.strip().startswith(("[", ";")) for ln in lines)


def _attrs(p: Path, hidden_system: bool):
    if not IS_WIN:
        return
    FILE_ATTRIBUTE_HIDDEN, FILE_ATTRIBUTE_SYSTEM, NORMAL = 0x2, 0x4, 0x80
    ctypes.windll.kernel32.SetFileAttributesW(str(p), (FILE_ATTRIBUTE_HIDDEN | FILE_ATTRIBUTE_SYSTEM)
                                              if hidden_system else NORMAL)


def _notify(folder: Path):
    if not IS_WIN:
        return
    SHCNE_UPDATEDIR, SHCNE_UPDATEITEM, SHCNF_PATHW, SHCNF_FLUSHNOWAIT = 0x1000, 0x2000, 0x0005, 0x3000
    sh = ctypes.windll.shell32
    for ev in (SHCNE_UPDATEITEM, SHCNE_UPDATEDIR):
        sh.SHChangeNotify(ev, SHCNF_PATHW | SHCNF_FLUSHNOWAIT, ctypes.c_wchar_p(str(folder)), None)
    sh.SHChangeNotify(0x08000000, 0, None, None)        # SHCNE_ASSOCCHANGED: refresh the icon cache


def _write_ini(ini: Path, lines: list[str], enc: str):
    if ini.exists():
        _attrs(ini, False)
    text = "\r\n".join(lines) + "\r\n"
    if enc == "utf-16" or any(ord(ch) > 127 for ch in text):
        ini.write_bytes(b"\xff\xfe" + text.encode("utf-16-le"))
    else:
        ini.write_bytes(text.encode("ascii"))
    _attrs(ini, True)


# ---------------------------------------------------------------- apply / reset
def apply(folder: str | Path, entry: IconEntry) -> None:
    folder = Path(folder)
    if not folder.is_dir():
        raise UserError(f"{folder.name} isn't a folder.")
    img = entry.image(SIZE)
    try:
        if IS_WIN:
            _apply_windows(folder, img)
        elif IS_MAC:
            _apply_mac(folder, img)
        else:
            _apply_linux(folder, img)
    except PermissionError:
        raise UserError(f"Windows doesn't let DragShift change {folder.name} (no write access).") from None


def _apply_windows(folder: Path, img: QImage):
    for old in folder.glob(ICON_PREFIX + "*.ico"):
        _attrs(old, False)
        old.unlink(missing_ok=True)
    token = os.urandom(5).hex()
    name = f"{ICON_PREFIX}{token}.ico"           # a new name every time: Explorer's icon cache can't hold on
    ico = folder / name
    write_ico(img, ico)
    _attrs(ico, True)
    ini = folder / "desktop.ini"
    lines, enc = _read_ini(ini)
    _write_ini(ini, ini_set_icon(lines, f"{name},0"), enc)
    if IS_WIN:
        ctypes.windll.shlwapi.PathMakeSystemFolderW(str(folder))
    _notify(folder)


def _apply_mac(folder: Path, img: QImage):
    from . import platform_mac
    tmp = library_dir() / ".apply.png"
    img.save(str(tmp))
    if not platform_mac.set_file_icon(folder, tmp):
        raise UserError("macOS didn't accept the icon.")
    tmp.unlink(missing_ok=True)


def _linux_store(img: QImage) -> Path:
    d = library_dir() / "applied"
    d.mkdir(exist_ok=True)
    tmp = d / ".tmp.png"
    img.save(str(tmp))
    p = d / f"{hashlib.sha1(tmp.read_bytes()).hexdigest()[:12]}.png"
    os.replace(tmp, p)
    return p


def _apply_linux(folder: Path, img: QImage):
    p = _linux_store(img)
    r = subprocess.run(["gio", "set", "-t", "string", str(folder), "metadata::custom-icon", p.as_uri()],
                       capture_output=True)
    if r.returncode != 0:
        raise UserError("Your file manager doesn't support custom folder icons (gio failed).")


def reset(folder: str | Path) -> None:
    folder = Path(folder)
    if IS_WIN:
        for old in folder.glob(ICON_PREFIX + "*.ico"):
            _attrs(old, False)
            old.unlink(missing_ok=True)
        ini = folder / "desktop.ini"
        if ini.exists():
            lines, enc = _read_ini(ini)
            lines = ini_set_icon(lines, None)
            if ini_is_empty(lines):
                _attrs(ini, False)
                ini.unlink(missing_ok=True)
                ctypes.windll.shlwapi.PathUnmakeSystemFolderW(str(folder))
            else:
                _write_ini(ini, lines, enc)
        _notify(folder)
    elif IS_MAC:
        from . import platform_mac
        platform_mac.set_file_icon(folder, None)
    else:
        subprocess.run(["gio", "set", "-t", "unset", str(folder), "metadata::custom-icon"], capture_output=True)


def current_icon(folder: str | Path) -> Path | None:
    """The DragShift icon a folder has now (Windows), for tests and the window."""
    for f in Path(folder).glob(ICON_PREFIX + "*.ico"):
        return f
    return None


# ---------------------------------------------------------------- window
from PySide6.QtCore import QSize, Signal  # noqa: E402
from PySide6.QtGui import QIcon, QPixmap  # noqa: E402
from PySide6.QtWidgets import (QButtonGroup, QColorDialog, QComboBox, QFileDialog, QHBoxLayout,  # noqa: E402
                               QLabel, QLineEdit, QListView, QListWidget, QListWidgetItem, QMenu,
                               QPushButton)

from .window import GlassWindow  # noqa: E402

IMAGE_DROP = {".png", ".jpg", ".jpeg", ".ico", ".webp", ".heic", ".bmp", ".gif", ".tif", ".tiff", ".svg"}


class IconGrid(QListWidget):
    """The library. Drop a folder onto an icon to apply it; drop pictures to add them."""
    folders_on_icon = Signal(list, object)       # (folders, IconEntry)
    folders_dropped = Signal(list)
    images_dropped = Signal(list)

    def __init__(self):
        super().__init__()
        self.setViewMode(QListView.IconMode)
        self.setResizeMode(QListView.Adjust)
        self.setMovement(QListView.Static)
        self.setIconSize(QSize(72, 72))
        self.setGridSize(QSize(104, 108))
        self.setWordWrap(True)
        self.setSpacing(4)
        self.setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DropOnly)
        self.viewport().setAcceptDrops(True)
        self.setStyleSheet("QListWidget { background: rgba(0,0,0,0.14); border: 1px solid rgba(255,255,255,0.18);"
                           " border-radius: 12px; padding: 6px; }"
                           "QListWidget::item { border-radius: 10px; padding: 4px; }"
                           "QListWidget::item:hover { background: rgba(255,255,255,0.12); }"
                           "QListWidget::item:selected { background: rgba(255,120,140,0.35); }")
        self._hover_row = -1

    @staticmethod
    def _paths(e) -> list[Path]:
        return [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dragMoveEvent(self, e):
        if not e.mimeData().hasUrls():
            return
        e.acceptProposedAction()
        it = self.itemAt(e.position().toPoint())
        if it is not None and any(p.is_dir() for p in self._paths(e)):
            self.setCurrentItem(it)

    def dropEvent(self, e):
        paths = self._paths(e)
        folders = [str(p) for p in paths if p.is_dir()]
        images = [str(p) for p in paths if p.is_file() and p.suffix.lower() in IMAGE_DROP]
        e.acceptProposedAction()
        if folders:
            it = self.itemAt(e.position().toPoint())
            if it is not None:
                self.folders_on_icon.emit(folders, it.data(Qt.UserRole))
            else:
                self.folders_dropped.emit(folders)
        if images:
            self.images_dropped.emit(images)


class FolderIconWindow(GlassWindow):
    def __init__(self, folders=(), notify=None):
        super().__init__("Folder icons", size=(760, 720))
        self.notify = notify
        self.targets: list[Path] = []
        self.setAcceptDrops(True)

        self.drop_lab = QLabel()
        self.drop_lab.setWordWrap(True)
        self.drop_lab.setStyleSheet("QLabel { background: rgba(255,255,255,0.07); border: 1.5px dashed "
                                    "rgba(255,255,255,0.35); border-radius: 12px; padding: 12px; }")
        self.body.addWidget(self.drop_lab)
        trow = QHBoxLayout()
        self.reset_btn = QPushButton("Reset to normal")
        self.reset_btn.setToolTip("Give the chosen folders their standard icon back")
        self.reset_btn.clicked.connect(self.reset_targets)
        self.clear_btn = QPushButton("Clear list")
        self.clear_btn.clicked.connect(lambda: self.set_targets([]))
        pick = QPushButton("Choose folder…")
        pick.clicked.connect(self.pick_folder)
        trow.addWidget(pick)
        trow.addStretch(1)
        trow.addWidget(self.clear_btn)
        trow.addWidget(self.reset_btn)
        self.body.addLayout(trow)

        self.grid = IconGrid()
        self.grid.itemClicked.connect(self.clicked)
        self.grid.folders_on_icon.connect(self.apply_to)
        self.grid.folders_dropped.connect(lambda f: self.set_targets(self.targets + [Path(x) for x in f]))
        self.grid.images_dropped.connect(self.add_images)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        self.body.addWidget(self.grid, 1)

        # make your own: colour + symbol or text
        mk = QHBoxLayout()
        mk.addWidget(QLabel("Make one:"))
        self.color = COLORS[6][1]
        self.swatches = QButtonGroup(self)
        for i, (n, c) in enumerate(COLORS[:10]):
            b = QPushButton()
            b.setObjectName("swatch")
            b.setCheckable(True)
            b.setChecked(c == self.color)
            b.setToolTip(n)
            b.setStyleSheet(f"background: {c};")
            b.clicked.connect(lambda _=False, c=c: self._set_color(c))
            self.swatches.addButton(b)
            mk.addWidget(b)
        more = QPushButton("…")
        more.setToolTip("Any colour")
        more.setFixedWidth(34)
        more.setStyleSheet("padding: 0;")
        more.clicked.connect(self._any_color)
        mk.addWidget(more)
        mk.addStretch(1)
        self.body.addLayout(mk)
        mk2 = QHBoxLayout()
        self.symbol = QComboBox()
        for s in SYMBOLS:
            self.symbol.addItem(SYMBOL_NAMES[s], s)
        self.symbol.currentIndexChanged.connect(self._preview)
        mk2.addWidget(self.symbol)
        self.text = QLineEdit()
        self.text.setPlaceholderText("or letters / emoji, e.g. 2026 or 🎬")
        self.text.setMaxLength(4)
        self.text.textChanged.connect(self._preview)
        mk2.addWidget(self.text, 1)
        self.prev = QLabel()
        self.prev.setFixedSize(52, 52)
        mk2.addWidget(self.prev)
        add = QPushButton("Add to library")
        add.clicked.connect(self.make_custom)
        mk2.addWidget(add)
        own = QPushButton("Add picture…")
        own.setToolTip("PNG, JPG, ICO, HEIC… – or just drop pictures onto the library")
        own.clicked.connect(self.pick_images)
        mk2.addWidget(own)
        self.body.addLayout(mk2)
        self.status = QLabel("")
        self.status.setObjectName("dim")
        self.body.addWidget(self.status)

        self.reload()
        self._preview()
        self.set_targets([Path(f) for f in folders if Path(f).is_dir()])

    # ---- library
    def reload(self):
        self.grid.clear()
        for e in all_icons():
            it = QListWidgetItem(QIcon(QPixmap.fromImage(e.image(144))), e.name)
            it.setData(Qt.UserRole, e)
            it.setToolTip(("Your icon" if not e.builtin else "Built-in") + " · drop a folder here or click")
            self.grid.addItem(it)

    def _set_color(self, c):
        self.color = c
        self._preview()

    def _any_color(self):
        c = QColorDialog.getColor(QColor(self.color), self, "Folder colour")
        if c.isValid():
            for b in self.swatches.buttons():
                b.setChecked(False)
            self._set_color(c.name())

    def _preview(self, *_):
        img = render_folder(self.color, self.symbol.currentData() or "", self.text.text().strip(), 104)
        self.prev.setPixmap(QPixmap.fromImage(img).scaled(52, 52, Qt.KeepAspectRatio, Qt.SmoothTransformation))

    def make_custom(self):
        e = save_custom(self.color, self.symbol.currentData() or "", self.text.text().strip())
        self.reload()
        self.status.setText(f"Added “{e.name}” – drop a folder on it.")
        if self.targets:
            self.apply_to([str(t) for t in self.targets], e)

    def add_images(self, files):
        added, bad = [], []
        for f in files:
            try:
                added.append(add_image(f))
            except UserError as ex:
                bad.append(str(ex))
        self.reload()
        self.status.setText((f"Added {len(added)} picture{'s' if len(added) != 1 else ''}. " if added else "")
                            + " ".join(bad))

    def pick_images(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Pictures for folder icons", "",
                                                "Images (*.png *.jpg *.jpeg *.ico *.webp *.heic *.bmp *.gif)")
        if files:
            self.add_images(files)

    def _menu(self, pos):
        it = self.grid.itemAt(pos)
        if it is None:
            return
        e: IconEntry = it.data(Qt.UserRole)
        m = QMenu(self)
        if self.targets:
            m.addAction(f"Use for {len(self.targets)} folder(s)", lambda: self.apply_to([str(t) for t in self.targets], e))
        m.addAction("Save as .ico…", lambda: self.export_ico(e))
        if not e.builtin:
            m.addAction("Remove from library", lambda: (remove_custom(e), self.reload()))
        m.exec(self.grid.mapToGlobal(pos))

    def export_ico(self, e: IconEntry):
        f, _ = QFileDialog.getSaveFileName(self, "Save icon", f"{e.name}.ico", "Icon (*.ico)")
        if f:
            write_ico(e.image(SIZE), Path(f))

    # ---- targets
    def set_targets(self, folders):
        seen, out = set(), []
        for f in folders:
            f = Path(f)
            if f.is_dir() and f not in seen:
                seen.add(f)
                out.append(f)
        self.targets = out
        if out:
            names = ", ".join(f.name or str(f) for f in out[:4]) + (f" +{len(out) - 4}" if len(out) > 4 else "")
            self.drop_lab.setText(f"<b>{len(out)} folder{'s' if len(out) != 1 else ''}:</b> {names}<br>"
                                  "Now click an icon below.")
        else:
            self.drop_lab.setText("<b>Drop folders here</b> and click an icon – or drop a folder straight onto an icon."
                                  "<br><span style='color:#c9c3d6'>Drop pictures onto the library to add your own.</span>")
        self.reset_btn.setEnabled(bool(out))
        self.clear_btn.setEnabled(bool(out))

    def pick_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Folder to change")
        if d:
            self.set_targets(self.targets + [Path(d)])

    def clicked(self, it):
        if not self.targets:
            self.status.setText("Drop a folder first (or drop it right onto the icon).")
            return
        self.apply_to([str(t) for t in self.targets], it.data(Qt.UserRole))

    def apply_to(self, folders, e: IconEntry):
        ok, errs = 0, []
        for f in folders:
            try:
                apply(f, e)
                ok += 1
            except UserError as ex:
                errs.append(str(ex))
            except OSError as ex:
                errs.append(f"{Path(f).name}: {ex.strerror or ex}")
        self.status.setText((f"✓ “{e.name}” set on {ok} folder{'s' if ok != 1 else ''}. " if ok else "")
                            + " ".join(errs[:2]))

    def reset_targets(self):
        for t in self.targets:
            try:
                reset(t)
            except OSError:
                pass
        self.status.setText(f"Standard icon restored on {len(self.targets)} folder(s).")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        paths = [Path(u.toLocalFile()) for u in e.mimeData().urls() if u.isLocalFile()]
        dirs = [p for p in paths if p.is_dir()]
        imgs = [str(p) for p in paths if p.is_file() and p.suffix.lower() in IMAGE_DROP]
        if dirs:
            self.set_targets(self.targets + dirs)
        if imgs:
            self.add_images(imgs)
