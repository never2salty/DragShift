"""3D models and CAD: STL, 3MF, OBJ, PLY, GLB/GLTF, OFF, STEP, OpenSCAD and DXF drawings.

Meshes go through trimesh, STEP through OpenCascade (cascadio), OpenSCAD files through the
OpenSCAD program (bundled with the Windows build) and DXF drawings through ezdxf.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .util import UserError, sibling

IS_WIN = sys.platform == "win32"
NO_WINDOW = 0x08000000 if IS_WIN else 0

MESH_EXTS = {".stl", ".3mf", ".obj", ".ply", ".glb", ".gltf", ".off"}
STEP_EXTS = {".step", ".stp"}
SCAD_EXTS = {".scad"}
DXF_EXTS = {".dxf"}
MODEL_EXTS = MESH_EXTS | STEP_EXTS | SCAD_EXTS

MODEL_TARGETS = ["STL", "3MF", "OBJ", "PLY", "GLB", "PNG"]
DXF_TARGETS = ["PDF", "PNG", "SVG"]
MODEL_TOOLS = ["Repair", "Simplify", "Center"]
EXT = {"STL": ".stl", "3MF": ".3mf", "OBJ": ".obj", "PLY": ".ply", "GLB": ".glb", "PNG": ".png",
       "PDF": ".pdf", "SVG": ".svg"}

MAX_RENDER_FACES = 120_000


def _trimesh():
    try:
        import trimesh  # type: ignore
        return trimesh
    except Exception as e:  # pragma: no cover
        raise UserError("3D support isn't installed (pip install trimesh).") from e


# ---------------------------------------------------------------- OpenSCAD
def openscad_exe() -> str:
    name = "openscad.exe" if IS_WIN else "openscad"
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    cands = [base / "openscad" / name, Path(__file__).resolve().parent / "bin" / "openscad" / name]
    if os.environ.get("DRAGSHIFT_OPENSCAD"):
        cands.insert(0, Path(os.environ["DRAGSHIFT_OPENSCAD"]))
    if IS_WIN:
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            if os.environ.get(env):
                cands.append(Path(os.environ[env]) / "OpenSCAD" / name)
                cands.append(Path(os.environ[env]) / "Programs" / "OpenSCAD" / name)
    for c in cands:
        if c.is_file():
            return str(c)
    found = shutil.which("openscad")
    if found:
        return found
    raise UserError("OpenSCAD wasn't found. The installer includes it; otherwise install it from openscad.org.")


def scad_to(src: Path, out: Path) -> Path:
    """Render a .scad file with OpenSCAD (STL/3MF/OFF output is chosen by the extension)."""
    tmp = out.with_name(f".~{out.stem}.part{out.suffix}")
    try:
        p = subprocess.run([openscad_exe(), "-o", str(tmp), str(src)], capture_output=True, timeout=1800,
                           stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, cwd=str(Path(src).parent))
        if p.returncode != 0 or not tmp.exists():
            err = (p.stderr or b"").decode("utf-8", "replace").strip().splitlines()
            msg = next((l for l in reversed(err) if "ERROR" in l or "error" in l), err[-1] if err else "")
            if "Current top level object is not a 3D object" in "\n".join(err):
                raise UserError("This OpenSCAD file draws 2D shapes only; extrude it to export a 3D model.")
            raise UserError(f"OpenSCAD: {msg or 'rendering failed'}")
        os.replace(tmp, out)
        return out
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- loading
def load_mesh(src: str | Path):
    """Any supported model → one trimesh.Trimesh (scenes are flattened, units kept in mm)."""
    tm = _trimesh()
    src = Path(src)
    ext = src.suffix.lower()
    if ext in SCAD_EXTS:
        with tempfile.TemporaryDirectory() as d:
            stl = scad_to(src, Path(d) / "model.stl")
            return tm.load(str(stl), force="mesh")
    if ext in STEP_EXTS:
        try:
            import cascadio  # type: ignore
        except Exception as e:  # pragma: no cover
            raise UserError("STEP support isn't installed (pip install cascadio).") from e
        with tempfile.TemporaryDirectory() as d:
            glb = Path(d) / "model.glb"
            cascadio.step_to_glb(str(src), str(glb), 0.05, 0.3)
            if not glb.exists():
                raise UserError("OpenCascade couldn't read this STEP file.")
            scene = tm.load(str(glb), force="scene")
            mesh = scene.to_geometry() if hasattr(scene, "to_geometry") else scene.dump(concatenate=True)
            # glTF is in metres; STEP files are almost always millimetres
            if mesh.extents.max() < 5:
                mesh.apply_scale(1000.0)
            return mesh
    try:
        m = tm.load(str(src), force="mesh")
    except Exception as e:
        raise UserError(f"Couldn't read this model: {e}") from e
    if m is None or not hasattr(m, "faces") or len(m.faces) == 0:
        raise UserError("This file contains no 3D surfaces.")
    return m


def _export(mesh, out: Path, target: str) -> Path:
    tmp = out.with_name(f".~{out.stem}.part{out.suffix}")
    try:
        data = mesh.export(file_type=target.lower())
        if isinstance(data, str):
            data = data.encode("utf-8")
        tmp.write_bytes(data)
        os.replace(tmp, out)
        return out
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------- preview render
def render_png(mesh, out: Path, size: int = 1200) -> Path:
    """Shaded isometric picture of the model (software renderer: no GPU/OpenGL needed)."""
    import numpy as np
    from PIL import Image, ImageDraw
    m = mesh
    if len(m.faces) > MAX_RENDER_FACES:
        try:
            m = m.simplify_quadric_decimation(face_count=MAX_RENDER_FACES)
        except Exception:
            idx = np.random.default_rng(0).choice(len(m.faces), MAX_RENDER_FACES, replace=False)
            m = m.submesh([idx], append=True)
    try:
        if not m.is_winding_consistent:
            import trimesh  # type: ignore
            m = m.copy()
            trimesh.repair.fix_normals(m)
        cull = m.is_watertight or m.is_winding_consistent
    except Exception:
        cull = False
    v = np.asarray(m.vertices, dtype=float)
    v = v - (v.min(0) + v.max(0)) / 2
    # isometric-ish view: rotate around Z by -45°, then tilt the Z-up model towards the viewer
    az, el = np.radians(-45), np.radians(35.264)
    rz = np.array([[np.cos(az), -np.sin(az), 0], [np.sin(az), np.cos(az), 0], [0, 0, 1]])
    rx = np.array([[1, 0, 0], [0, np.cos(el), -np.sin(el)], [0, np.sin(el), np.cos(el)]])
    v = v @ rz.T @ rx.T                      # x right, y towards viewer(depth), z up
    tri = v[np.asarray(m.faces)]
    n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    ln = np.linalg.norm(n, axis=1)
    keep = ln > 0
    tri, n = tri[keep], n[keep] / ln[keep, None]
    if cull:  # the viewer sits at -y: drop faces turned away (fixes painter's-order artefacts)
        front = n[:, 1] < 0
        if front.sum() > 0.2 * len(front):
            tri, n = tri[front], n[front]
    light = np.array([-0.4, -0.7, 0.6])
    light /= np.linalg.norm(light)
    shade = 0.35 + 0.65 * np.abs(n @ light)
    depth = tri[:, :, 1].max(1) * 0.5 + tri[:, :, 1].mean(1) * 0.5
    order = np.argsort(-depth)              # far first (painter's algorithm)
    ss = 2
    pad = 0.08
    w = size * ss
    xy = tri[:, :, [0, 2]]
    lo, hi = xy.reshape(-1, 2).min(0), xy.reshape(-1, 2).max(0)
    scale = (1 - 2 * pad) * w / max(1e-9, (hi - lo).max())
    off = np.array([w, w]) / 2 - (lo + hi) / 2 * scale
    px = xy * scale + off
    px[:, :, 1] = w - px[:, :, 1]
    img = Image.new("RGB", (w, w), (244, 245, 248))
    d = ImageDraw.Draw(img)
    base = np.array([96, 140, 230])
    cols = (base[None, :] * shade[:, None] + 255 * 0.08).clip(0, 255).astype(int)
    for i in order:
        c = tuple(cols[i])
        d.polygon([tuple(p) for p in px[i]], fill=c, outline=c)
    img = img.resize((size, size), Image.LANCZOS)
    from .convert import atomic_save
    atomic_save(lambda dst: img.save(dst, "PNG", optimize=True), out)
    return out


# ---------------------------------------------------------------- DXF drawings
def dxf_to(src: str | Path, target: str) -> list[Path]:
    try:
        import ezdxf  # type: ignore
        from ezdxf.addons.drawing import Frontend, RenderContext, layout
        from ezdxf.addons.drawing import pymupdf as pm_backend
        from ezdxf.addons.drawing import svg as svg_backend
    except Exception as e:  # pragma: no cover
        raise UserError("DXF support isn't installed (pip install ezdxf).") from e
    target = target.upper()
    try:
        doc = ezdxf.readfile(str(src))
    except Exception as e:
        raise UserError(f"Couldn't read this DXF drawing: {e}") from e
    msp = doc.modelspace()
    from ezdxf.addons.drawing.config import BackgroundPolicy, ColorPolicy, Configuration
    ctx = RenderContext(doc)
    cfg = Configuration(background_policy=BackgroundPolicy.WHITE, color_policy=ColorPolicy.BLACK)
    out = sibling(src, ext=EXT[target])
    page = layout.Page(297, 210, layout.Units.mm, margins=layout.Margins.all(10))  # A4 landscape
    settings = layout.Settings(fit_page=True)
    if target == "SVG":
        be = svg_backend.SVGBackend()
        Frontend(ctx, be, config=cfg).draw_layout(msp, finalize=True)
        out.write_text(be.get_string(page, settings=settings), encoding="utf-8")
        return [out]
    be = pm_backend.PyMuPdfBackend()
    Frontend(ctx, be, config=cfg).draw_layout(msp, finalize=True)
    if target == "PDF":
        out.write_bytes(be.get_pdf_bytes(page, settings=settings))
    else:
        out.write_bytes(be.get_pixmap_bytes(page, fmt="png", settings=settings, dpi=150))
    return [out]


# ---------------------------------------------------------------- dispatch
def targets_for(cat: str, ext: str) -> list[str]:
    ext = ext.lower()
    if cat == "dxf":
        return DXF_TARGETS
    same = {".stl": "STL", ".3mf": "3MF", ".obj": "OBJ", ".ply": "PLY", ".glb": "GLB"}.get(ext)
    return [t for t in MODEL_TARGETS if t != same]


def convert(src: str | Path, cat: str, target: str) -> list[Path]:
    target = target.upper()
    src = Path(src)
    if cat == "dxf":
        return dxf_to(src, target)
    out = sibling(src, ext=EXT[target])
    if src.suffix.lower() in SCAD_EXTS and target in ("STL", "3MF"):
        try:
            return [scad_to(src, out)]
        except UserError:
            if target == "STL":
                raise
    mesh = load_mesh(src)
    if target == "PNG":
        return [render_png(mesh, out)]
    return [_export(mesh, out, target)]


def tool(src: str | Path, name: str) -> list[Path]:
    src = Path(src)
    mesh = load_mesh(src)
    ext = src.suffix.lower() if src.suffix.lower() in (".stl", ".3mf", ".obj", ".ply", ".glb") else ".stl"
    target = ext.lstrip(".").upper()
    if name == "Repair":
        import trimesh  # type: ignore
        mesh.merge_vertices()
        mesh.update_faces(mesh.nondegenerate_faces())
        mesh.update_faces(mesh.unique_faces())
        mesh.remove_unreferenced_vertices()
        trimesh.repair.fix_normals(mesh)
        trimesh.repair.fill_holes(mesh)
        out = sibling(src, "Repaired", ext)
        return [_export(mesh, out, target)]
    if name == "Simplify":
        n = len(mesh.faces)
        if n < 500:
            raise UserError(f"This model has only {n} triangles; nothing to simplify.")
        try:
            small = mesh.simplify_quadric_decimation(face_count=max(250, n // 2))
        except Exception as e:
            raise UserError(f"Couldn't simplify: {e}") from e
        out = sibling(src, "Simplified", ext)
        return [_export(small, out, target)]
    if name == "Center":
        b = mesh.bounds
        mesh.apply_translation([-(b[0][0] + b[1][0]) / 2, -(b[0][1] + b[1][1]) / 2, -b[0][2]])
        out = sibling(src, "Centered", ext)
        return [_export(mesh, out, target)]
    raise ValueError(name)


def describe(path: str | Path) -> str:
    """'120 × 80 × 35 mm · 24 k triangles' for the notification."""
    try:
        m = load_mesh(path)
        x, y, z = m.extents
        t = len(m.faces)
        tri = f"{t / 1000:.0f} k triangles" if t >= 10_000 else f"{t} triangles"
        return f"{x:.1f} × {y:.1f} × {z:.1f} mm · {tri}"
    except Exception:
        return ""
