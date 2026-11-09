"""TEMPORARY diagnostics 4: each stage of a graded screenshot on this FFmpeg."""
import subprocess

from PIL import Image

from dragshift import framegrab, media
from dragshift import grade as G


def test_diag(tmp_path):
    ff = media.ffmpeg_exe()
    w, h = 320, 240
    p = tmp_path / "g.mp4"
    y = int(round(16 + 0.4106 * 219))
    frame = bytes([y]) * (w * h) + bytes([128]) * (w * h // 2)
    proc = subprocess.Popen([ff, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
                             "-r", "25", "-i", "-", "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p",
                             "-colorspace", "bt709", str(p)], stdin=subprocess.PIPE)
    for _ in range(25):
        proc.stdin.write(frame)
    proc.stdin.close()
    proc.wait()
    g = G.Grade(profile="slog3cine", lut=G.BUILTIN)
    info = media.probe(p)
    res = [f"vf={framegrab.frame_filter(info, False, g)}"]
    lut = G.lut3d_filter(g, 65, "tetrahedral")[0]

    def run(vf, ext="png", extra=()):
        out = tmp_path / f"o.{ext}"
        out.unlink(missing_ok=True)
        r = media._run_quick(["-loglevel", "error", "-y", "-i", str(p), "-frames:v", "1", "-vf", vf, *extra, str(out)],
                             cwd=G.cube_dir())
        return Image.open(out).convert("RGB").getpixel((160, 120)) if r.returncode == 0 else r.stderr.decode()[-90:]
    stages = {
        "decode": "format=rgb48le",
        "scale": "scale=iw*sar:ih:flags=lanczos,setsar=1,format=rgb48le",
        "lut65tet": f"format=rgb48le,{lut}",
        "lut65tri": "format=rgb48le," + G.lut3d_filter(g, 65, "trilinear")[0],
        "lut33tet": "format=rgb48le," + G.lut3d_filter(g, 33, "tetrahedral")[0],
        "full_png": framegrab.frame_filter(info, False, g),
    }
    for k, vf in stages.items():
        res.append(f"{k}={run(vf)}")
    res.append(f"full_jpg={run(framegrab.frame_filter(info, False, g)[:-len('format=rgb24')] + 'scale=out_color_matrix=bt601:out_range=pc,format=yuvj444p', 'jpg', ['-c:v', 'mjpeg', '-q:v', '2'])}")
    res.append(f"save_png={Image.open(framegrab.save_frame(p, 0.5, 'PNG', grade=g)).convert('RGB').getpixel((160, 120))}")
    res.append(f"save_jpg={Image.open(framegrab.save_frame(p, 0.5, 'JPG', grade=g)).convert('RGB').getpixel((160, 120))}")
    t = G.table_for(g, 65)
    i = int(round(y_full := (y - 16) / 219 * 64))
    res.append(f"tab65[{i}]={t[i, i, i].round(4).tolist()}")
    assert False, " | ".join(map(str, res))
