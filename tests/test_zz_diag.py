"""TEMPORARY diagnostics: input decode vs lut3d on this FFmpeg."""
import subprocess

from PIL import Image

from dragshift import media
from dragshift import grade as G


def test_diag(tmp_path):
    ff = media.ffmpeg_exe()
    res = []
    # 1) exact grey PNG straight into lut3d (no video decode at all)
    src = tmp_path / "grey.png"
    Image.new("RGB", (64, 64), (105, 105, 105)).save(src)
    for g, tag in [(G.Grade(profile="slog3cine", lut=G.BUILTIN), "builtin"), (G.Grade(saturation=0.5), "sat")]:
        name = G.cube_file(g, 33).name
        for fmt in ("rgb24", "rgb48le", "gbrpf32le"):
            out = tmp_path / f"{tag}_{fmt}.png"
            r = media._run_quick(["-loglevel", "error", "-i", str(src), "-vf",
                                  f"format={fmt},lut3d=file={name},format=rgb24", str(out)], cwd=G.cube_dir())
            res.append(f"{tag}/{fmt}=" + (str(Image.open(out).convert("RGB").getpixel((32, 32))) if out.exists() else r.stderr.decode()[-120:]))
    # 2) a cube written by hand: identity 2x2x2 and a 'grey→0.5' constant
    ident = G.cube_dir() / "ident2.cube"
    ident.write_text("LUT_3D_SIZE 2\n" + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n")
    out = tmp_path / "ident.png"
    media._run_quick(["-loglevel", "error", "-i", str(src), "-vf", "lut3d=file=ident2.cube", str(out)], cwd=G.cube_dir())
    res.append(f"ident2={Image.open(out).convert('RGB').getpixel((32, 32)) if out.exists() else 'x'}")
    # 3) table neighbourhood around grey
    tab = G.table_for(G.Grade(profile="slog3cine", lut=G.BUILTIN), 33)
    i = int(105 / 255 * 32)
    res.append(f"tab[{i}]={tab[i, i, i].round(3).tolist()} tab[{i+1}]={tab[i+1, i+1, i+1].round(3).tolist()}")
    lines = open(G.cube_file(G.Grade(profile="slog3cine", lut=G.BUILTIN), 33)).read().splitlines()
    k = 4 + i * 33 * 33 + i * 33 + i
    res.append(f"cube line {k}: {lines[k]!r}")
    assert False, " | ".join(res)
