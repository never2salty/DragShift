"""TEMPORARY: find which lut3d variant gives a colour cast with this FFmpeg."""
import subprocess

import numpy as np
from PIL import Image

from dragshift import framegrab, media
from dragshift import grade as G


def test_diag(tmp_path):
    ff = media.ffmpeg_exe()
    p = tmp_path / "grey.mp4"
    y = int(round(16 + 0.4106 * 219))
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=gray:s=320x240:r=25:d=1",
                    "-vf", f"geq=lum={y}:cb=128:cr=128", "-c:v", "libx264", "-crf", "4", "-pix_fmt", "yuv420p",
                    "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", str(p)], check=True)
    g = G.Grade(profile="slog3cine", lut=G.BUILTIN)
    res = [media._run_quick(["-version"]).stdout.decode().splitlines()[0][:40]]
    for size in (33, 65):
        name = G.cube_file(g, size).name
        for fmt in ("rgb24", "rgb48le", "gbrp16le", "gbrpf32le"):
            for interp in ("tetrahedral", "trilinear", "nearest"):
                out = tmp_path / f"o_{size}_{fmt}_{interp}.png"
                r = media._run_quick(["-loglevel", "error", "-i", str(p), "-frames:v", "1", "-vf",
                                      f"format={fmt},lut3d=file={name}:interp={interp},format=rgb24", str(out)],
                                     cwd=G.cube_dir())
                v = list(Image.open(out).convert("RGB").getpixel((160, 120))) if out.exists() else r.stderr[-80:]
                res.append(f"{size}/{fmt}/{interp}={v}")
    tab = G.table_for(g, 33)
    i = int(round(0.4106 * 32))
    res.append(f"table diag {tab[i, i, i].round(3).tolist()} cube head {open(G.cube_file(g, 33)).read(120)!r}")
    assert False, " | ".join(map(str, res))
