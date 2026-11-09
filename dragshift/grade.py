"""Colour grading for log footage (Sony S-Log3, Fujifilm F-Log/F-Log2) and normal video.

Everything a grade does is baked into ONE 3D LUT:
  camera log → linear → white balance + exposure (in stops, on the scene light)
  → your .cube LUT (or the built-in camera → Rec.709 conversion) at a chosen strength
  → contrast, highlights, shadows, saturation (on the finished picture).
The same LUT drives the still preview (numpy), playback, screenshots and exports (FFmpeg lut3d),
so what you see is what you save.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import numpy as np

from .util import UserError

# ---------------------------------------------------------------- camera profiles
PROFILES = {
    "rec709": "Standard (Rec.709)",
    "slog3cine": "Sony S-Log3 · S-Gamut3.Cine",
    "slog3": "Sony S-Log3 · S-Gamut3",
    "flog": "Fujifilm F-Log",
    "flog2": "Fujifilm F-Log2",
}
LOG_PROFILES = {"slog3cine", "slog3", "flog", "flog2"}

BUILTIN = "builtin"      # lut field value: camera → Rec.709 conversion made by DragShift
NO_LUT = ""

_XYZ_TO_709 = np.array([[3.2404542, -1.5371385, -0.4985314],
                        [-0.9692660, 1.8760108, 0.0415560],
                        [0.0556434, -0.2040259, 1.0572252]])
_GAMUT_TO_XYZ = {
    "slog3cine": np.array([[0.5990839208, 0.2489255161, 0.1024464902],
                           [0.2150758201, 0.8850685017, -0.1001443219],
                           [-0.0320658495, -0.0276583907, 1.1487819910]]),
    "slog3": np.array([[0.7064827132, 0.1288010498, 0.1151721641],
                       [0.2709796708, 0.7866064112, -0.0575860820],
                       [-0.0096778454, 0.0046000375, 1.0941355587]]),
}
_2020_TO_709 = np.array([[1.6605, -0.5876, -0.0728],
                         [-0.1246, 1.1329, -0.0083],
                         [-0.0182, -0.1006, 1.1187]])

# F-Log / F-Log2 constants (Fujifilm data sheets)
_FLOG = {"flog": (0.555556, 0.009468, 0.344676, 0.790453, 8.735631, 0.092864, 0.100537775223865, 0.00089),
         "flog2": (5.555556, 0.064829, 0.245281, 0.384316, 8.799461, 0.092864, 0.100686685370811, 0.000889)}


def log_to_linear(v: np.ndarray, profile: str) -> np.ndarray:
    v = np.asarray(v, dtype=np.float64)
    if profile in ("slog3", "slog3cine"):
        hi = 10.0 ** ((v * 1023.0 - 420.0) / 261.5) * (0.18 + 0.01) - 0.01
        lo = (v * 1023.0 - 95.0) * 0.01125000 / (171.2102946929 - 95.0)
        return np.where(v >= 171.2102946929 / 1023.0, hi, lo)
    if profile in _FLOG:
        a, b, c, d, e, f, cut2, _ = _FLOG[profile]
        return np.where(v >= cut2, (10.0 ** ((v - d) / c) - b) / a, (v - f) / e)
    # Rec.709 video: close enough to a 2.4 display gamma for exposure maths
    return np.clip(v, 0, None) ** 2.4


def linear_to_log(x: np.ndarray, profile: str) -> np.ndarray:
    x = np.asarray(x, dtype=np.float64)
    if profile in ("slog3", "slog3cine"):
        hi = (420.0 + np.log10((np.maximum(x, 0.01125) + 0.01) / (0.18 + 0.01)) * 261.5) / 1023.0
        lo = (x * (171.2102946929 - 95.0) / 0.01125000 + 95.0) / 1023.0
        return np.where(x >= 0.01125, hi, lo)
    if profile in _FLOG:
        a, b, c, d, e, f, _, cut1 = _FLOG[profile]
        return np.where(x >= cut1, c * np.log10(np.maximum(a * x + b, 1e-9)) + d, e * x + f)
    return np.clip(x, 0, None) ** (1 / 2.4)


def camera_to_709(lin: np.ndarray, profile: str) -> np.ndarray:
    """Scene-linear camera gamut → scene-linear Rec.709 primaries."""
    if profile in _GAMUT_TO_XYZ:
        m = _XYZ_TO_709 @ _GAMUT_TO_XYZ[profile]
    elif profile in _FLOG:
        m = _2020_TO_709          # F-Gamut uses Rec.2020 primaries
    else:
        return lin
    return lin @ m.T


def filmic(x: np.ndarray) -> np.ndarray:
    """Scene-linear → display-linear, a gentle film curve (ACES-like fit) with 18 % grey ≈ 48 %."""
    x = np.maximum(x * 0.7, 0)
    return np.clip((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0, 1)


def display_encode(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0, 1) ** (1 / 2.4)


# ---------------------------------------------------------------- .cube files
@dataclass
class Cube:
    table: np.ndarray            # (N, N, N, 3) indexed [b, g, r], or (N, 3) for a 1D LUT
    lo: np.ndarray
    hi: np.ndarray
    title: str = ""

    @property
    def is_3d(self) -> bool:
        return self.table.ndim == 4


_CUBE_CACHE: dict[tuple[str, float], Cube] = {}


def read_cube(path: str | Path) -> Cube:
    path = Path(path)
    try:
        key = (str(path), path.stat().st_mtime)
    except OSError:
        raise UserError(f"The LUT {path.name} was moved or deleted.") from None
    if key in _CUBE_CACHE:
        return _CUBE_CACHE[key]
    size3 = size1 = 0
    lo, hi = np.zeros(3), np.ones(3)
    title = ""
    rows: list[list[float]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        raise UserError(f"Couldn't read the LUT: {e}") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        head = line.split()[0].upper()
        if head == "TITLE":
            title = line[5:].strip().strip('"')
        elif head == "LUT_3D_SIZE":
            size3 = int(line.split()[1])
        elif head == "LUT_1D_SIZE":
            size1 = int(line.split()[1])
        elif head == "DOMAIN_MIN":
            lo = np.array([float(v) for v in line.split()[1:4]])
        elif head == "DOMAIN_MAX":
            hi = np.array([float(v) for v in line.split()[1:4]])
        elif head[0].isdigit() or head[0] in "-.":
            parts = line.split()
            if len(parts) >= 3:
                rows.append([float(parts[0]), float(parts[1]), float(parts[2])])
    data = np.array(rows, dtype=np.float64)
    if size3:
        if len(data) < size3 ** 3:
            raise UserError(f"{path.name} is incomplete ({len(data)} of {size3 ** 3} entries).")
        table = data[: size3 ** 3].reshape(size3, size3, size3, 3)
    elif size1:
        table = data[:size1]
    else:
        raise UserError(f"{path.name} isn't a .cube LUT (no LUT_3D_SIZE).")
    cube = Cube(table, lo, hi, title or path.stem)
    _CUBE_CACHE[key] = cube
    return cube


def apply_cube(cube: Cube, rgb: np.ndarray) -> np.ndarray:
    """Trilinear lookup of float rgb (..., 3)."""
    x = (rgb - cube.lo) / np.maximum(cube.hi - cube.lo, 1e-9)
    x = np.clip(x, 0.0, 1.0)
    if not cube.is_3d:
        n = len(cube.table)
        f = x * (n - 1)
        i0 = np.clip(np.floor(f).astype(np.int64), 0, n - 2)
        t = f - i0
        out = np.empty_like(x)
        for c in range(3):
            a = cube.table[i0[..., c], c]
            b = cube.table[i0[..., c] + 1, c]
            out[..., c] = a + (b - a) * t[..., c]
        return out
    return trilinear(cube.table, x)


def trilinear(table: np.ndarray, x: np.ndarray) -> np.ndarray:
    """table (N,N,N,3) indexed [b,g,r]; x (...,3) in 0..1."""
    n = table.shape[0]
    f = x * (n - 1)
    i = np.clip(np.floor(f).astype(np.int32), 0, n - 2)
    t = (f - i).astype(np.float32)
    r0, g0, b0 = i[..., 0], i[..., 1], i[..., 2]
    tr, tg, tb = t[..., 0:1], t[..., 1:2], t[..., 2:3]
    tab = table.astype(np.float32, copy=False)

    def at(db, dg, dr):
        return tab[b0 + db, g0 + dg, r0 + dr]
    c00 = at(0, 0, 0) * (1 - tr) + at(0, 0, 1) * tr
    c01 = at(0, 1, 0) * (1 - tr) + at(0, 1, 1) * tr
    c10 = at(1, 0, 0) * (1 - tr) + at(1, 0, 1) * tr
    c11 = at(1, 1, 0) * (1 - tr) + at(1, 1, 1) * tr
    c0 = c00 * (1 - tg) + c01 * tg
    c1 = c10 * (1 - tg) + c11 * tg
    return c0 * (1 - tb) + c1 * tb


def write_cube(table: np.ndarray, path: Path, title: str = "DragShift grade") -> Path:
    n = table.shape[0]
    flat = table.reshape(-1, 3)  # [b][g][r] order with r fastest = .cube order
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {n}", "DOMAIN_MIN 0 0 0", "DOMAIN_MAX 1 1 1"]
    body = "\n".join(f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in np.clip(flat, 0, 1))
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n" + body + "\n", encoding="ascii")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------- the grade
@dataclass
class Grade:
    profile: str = "rec709"
    lut: str = NO_LUT           # path to a .cube, BUILTIN, or "" for none
    strength: float = 1.0       # 0..1
    exposure: float = 0.0       # stops, −4..+4
    contrast: float = 0.0       # −1..1
    highlights: float = 0.0     # −1..1
    shadows: float = 0.0        # −1..1
    saturation: float = 1.0     # 0..2
    temperature: float = 0.0    # −1 (cool) .. 1 (warm)
    tint: float = 0.0           # −1 (green) .. 1 (magenta)

    def is_identity(self) -> bool:
        return self == Grade(profile=self.profile) and (self.profile == "rec709")

    def key(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_dict(cls, d: dict) -> "Grade":
        names = {f.name for f in fields(cls)}
        g = cls(**{k: v for k, v in (d or {}).items() if k in names})
        if g.profile not in PROFILES:
            g.profile = "rec709"
        return g

    def lut_name(self) -> str:
        if self.lut == BUILTIN:
            return "Camera → Rec.709"
        return Path(self.lut).stem if self.lut else "No LUT"


def _wb_gains(temp: float, tint: float) -> np.ndarray:
    g = np.array([2 ** (0.35 * temp), 2 ** (-0.25 * tint), 2 ** (-0.35 * temp)])
    return g / (0.2126 * g[0] + 0.7152 * g[1] + 0.0722 * g[2])


def _luma(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def evaluate(g: Grade, rgb: np.ndarray) -> np.ndarray:
    """The whole grade on float rgb in 0..1 (video levels already expanded to full range)."""
    rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0, 1)
    prof = g.profile
    lut = g.lut if (g.lut != BUILTIN or prof in LOG_PROFILES) else NO_LUT
    gains = _wb_gains(g.temperature, g.tint) * (2.0 ** g.exposure)
    scene_touched = g.exposure != 0 or g.temperature != 0 or g.tint != 0

    if lut == BUILTIN:
        lin = camera_to_709(log_to_linear(rgb, prof), prof) * gains
        graded = display_encode(filmic(lin))
        base = rgb
    else:
        if scene_touched:
            lin = log_to_linear(rgb, prof) * gains
            src = np.clip(linear_to_log(lin, prof), 0, 1)
        else:
            src = rgb
        base = src
        graded = apply_cube(read_cube(lut), src) if lut else src
    if g.strength < 1 and lut:
        graded = base + (graded - base) * g.strength
    out = np.clip(graded, 0, 1)

    # finishing on the display picture
    if g.contrast:
        k = 1.0 + g.contrast * (0.9 if g.contrast > 0 else 0.6)
        p = 0.45
        lo = p * (np.maximum(out, 0) / p) ** k
        hi = 1 - (1 - p) * (np.maximum(1 - out, 0) / (1 - p)) ** k
        out = np.where(out < p, lo, hi)
    if g.highlights or g.shadows:
        L = _luma(out)
        dL = np.zeros_like(L)
        if g.highlights:
            m = L > 0.4
            dL = dL + np.where(m, g.highlights * 0.75 * (L - 0.4) * (1 - L), 0)
        if g.shadows:
            m = L < 0.6
            dL = dL + np.where(m, g.shadows * 0.9 * L * (1 - L / 0.6) ** 2, 0)
        newL = np.clip(L + dL, 0, 1)
        scale = np.where(L > 1e-4, newL / np.maximum(L, 1e-4), 1.0)
        out = out * scale[..., None]
        flat = L <= 1e-4
        if np.any(flat):
            out[flat] = newL[flat][..., None]
    if g.saturation != 1:
        L = _luma(out)[..., None]
        out = L + (out - L) * g.saturation
    return np.clip(out, 0, 1)


def build_table(g: Grade, size: int = 33) -> np.ndarray:
    ax = np.linspace(0.0, 1.0, size)
    b, gg, r = np.meshgrid(ax, ax, ax, indexing="ij")
    rgb = np.stack([r, gg, b], axis=-1)       # [b][g][r] layout, r fastest
    return evaluate(g, rgb).astype(np.float32)


_TABLES: dict[tuple[str, int], np.ndarray] = {}
_TLOCK = threading.Lock()


def table_for(g: Grade, size: int = 33) -> np.ndarray:
    k = (g.key() + (str(Path(g.lut).stat().st_mtime) if g.lut not in (NO_LUT, BUILTIN) and os.path.exists(g.lut) else ""), size)
    with _TLOCK:
        t = _TABLES.get(k)
    if t is None:
        t = build_table(g, size)
        with _TLOCK:
            if len(_TABLES) > 24:
                _TABLES.clear()
            _TABLES[k] = t
    return t


def apply_to_image(g: Grade, rgb8: np.ndarray) -> np.ndarray:
    """uint8 (H, W, 3) → graded uint8, for the instant still preview."""
    tab = table_for(g, 33)
    x = rgb8.astype(np.float32) * (1 / 255)
    return (trilinear(tab, x) * 255 + 0.5).clip(0, 255).astype(np.uint8)


def cube_dir() -> Path:
    d = Path(tempfile.gettempdir()) / "DragShift-grades"
    d.mkdir(parents=True, exist_ok=True)
    return d


def cube_file(g: Grade, size: int = 33) -> Path:
    """The grade as a .cube for FFmpeg (cached by content). Plain file name: used with cwd=cube_dir()."""
    import hashlib
    tab = table_for(g, size)
    h = hashlib.sha1(tab.tobytes()).hexdigest()[:16]
    p = cube_dir() / f"g{size}_{h}.cube"
    if not p.exists():
        write_cube(tab, p)
    return p


def lut3d_filter(g: Grade, size: int = 33, interp: str = "trilinear") -> tuple[str, Path]:
    """('lut3d=file=gXX.cube:interp=…', directory FFmpeg must run in)."""
    p = cube_file(g, size)
    return f"lut3d=file={p.name}:interp={interp}", p.parent


# ---------------------------------------------------------------- presets + recent LUTs
def _store_path() -> Path:
    from .history import history_path
    return history_path().with_name("grades.json")


_SLOCK = threading.Lock()


def _load() -> dict:
    try:
        d = json.loads(_store_path().read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    p = _store_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def presets() -> list[dict]:
    """[{'id', 'name', 'grade': {...}}], newest first."""
    return list(_load().get("presets", []))


def save_preset(name: str, g: Grade) -> dict:
    with _SLOCK:
        d = _load()
        items = [p for p in d.get("presets", []) if p.get("name", "").lower() != name.strip().lower()]
        entry = {"id": uuid.uuid4().hex, "name": name.strip(), "grade": asdict(g)}
        d["presets"] = [entry] + items
        _save(d)
    if g.lut not in (NO_LUT, BUILTIN):
        remember_lut(g.lut)
    return entry


def delete_preset(pid: str) -> None:
    with _SLOCK:
        d = _load()
        d["presets"] = [p for p in d.get("presets", []) if p.get("id") != pid]
        _save(d)


def recent_luts() -> list[str]:
    return [p for p in _load().get("recent_luts", []) if os.path.exists(p)]


def remember_lut(path: str) -> None:
    path = str(Path(path).resolve())
    with _SLOCK:
        d = _load()
        items = [p for p in d.get("recent_luts", []) if p != path]
        d["recent_luts"] = [path] + items[:11]
        _save(d)


def last_grade() -> Grade:
    return Grade.from_dict(_load().get("last", {}))


def remember_last(g: Grade) -> None:
    with _SLOCK:
        d = _load()
        d["last"] = asdict(g)
        _save(d)
