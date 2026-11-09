"""TEMPORARY diagnostics 3: how does this FFmpeg decode a grey YUV clip to RGB?"""
import subprocess

from PIL import Image

from dragshift import media


def test_diag(tmp_path):
    ff = media.ffmpeg_exe()
    res = []
    y = 106
    p = tmp_path / "g.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=25:d=1",
                    "-vf", f"format=yuv420p,geq=lum={y}:cb=128:cr=128", "-c:v", "libx264", "-crf", "4",
                    "-pix_fmt", "yuv420p", "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                    str(p)], check=True)
    raw = media._run_quick(["-loglevel", "error", "-i", str(p), "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"]).stdout
    n = 320 * 240
    res.append(f"yuv Y={raw[n//2 + 160]} U={raw[n + n//8 + 80]} V={raw[n + n//4 + n//8 + 80]}")
    info = media.probe(p)
    res.append("probe: " + [l for l in info.raw.splitlines() if "Video:" in l][0].strip()[:160])
    for vf in ["format=rgb24", "format=rgb48le", "scale=iw*sar:ih:flags=lanczos,setsar=1,format=rgb48le",
               "scale=flags=accurate_rnd+full_chroma_int,format=rgb24", "scale=in_color_matrix=bt709,format=rgb24",
               "scale=in_range=tv:in_color_matrix=bt709:flags=lanczos,format=rgb48le"]:
        out = tmp_path / "o.png"
        r = media._run_quick(["-loglevel", "error", "-y", "-i", str(p), "-frames:v", "1", "-vf", vf, str(out)])
        res.append(f"[{vf}]=" + (str(Image.open(out).convert("RGB").getpixel((160, 120))) if r.returncode == 0 else r.stderr.decode()[-100:]))
    # same thing without the colour tags
    p2 = tmp_path / "g2.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=gray:s=320x240:r=25:d=1",
                    "-vf", f"format=yuv420p,geq=lum={y}:cb=128:cr=128", "-c:v", "libx264", "-crf", "4",
                    "-pix_fmt", "yuv420p", str(p2)], check=True)
    out = tmp_path / "o2.png"
    media._run_quick(["-loglevel", "error", "-y", "-i", str(p2), "-frames:v", "1", "-vf", "format=rgb48le", str(out)])
    res.append(f"untagged rgb48={Image.open(out).convert('RGB').getpixel((160, 120))}")
    assert False, " | ".join(res)
