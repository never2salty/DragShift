"""Colour grading: log curves, .cube LUTs, presets, graded screenshots/exports, player integration."""
import subprocess
import time
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from dragshift import framegrab, media
from dragshift import grade as G
from dragshift.audioplay import atempo_chain

try:
    FF = media.ffmpeg_exe()
except Exception:  # pragma: no cover
    FF = None


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def write_lut(path: Path, fn, n=17):
    ax = np.linspace(0, 1, n)
    b, g, r = np.meshgrid(ax, ax, ax, indexing="ij")
    tab = fn(np.stack([r, g, b], -1))
    return G.write_cube(tab, path)


@pytest.fixture
def invert_lut(tmp_path):
    return write_lut(tmp_path / "Invert.cube", lambda x: 1 - x)


@pytest.fixture
def warm_lut(tmp_path):
    return write_lut(tmp_path / "Warm Look.cube", lambda x: np.clip(x * [1.1, 1.0, 0.85], 0, 1))


# ---------------------------------------------------------------- maths
@pytest.mark.parametrize("prof,grey", [("slog3", 0.4106), ("slog3cine", 0.4106), ("flog", 0.4593), ("flog2", 0.391)])
def test_log_curves_match_manufacturers(prof, grey):
    assert abs(float(G.linear_to_log(np.array(0.18), prof)) - grey) < 0.002
    x = np.array([0.0, 0.01, 0.18, 1.0, 5.0])
    assert np.allclose(G.log_to_linear(G.linear_to_log(x, prof), prof), x, atol=1e-6)


def test_exposure_is_one_stop_in_log():
    v = float(G.linear_to_log(np.array(0.18), "slog3"))
    up = G.evaluate(G.Grade(profile="slog3", exposure=1), np.array([[v, v, v]]))[0, 0]
    assert abs(float(G.log_to_linear(np.array(up), "slog3")) - 0.36) < 0.002


def test_builtin_conversion_mid_grey():
    for prof in ("slog3cine", "flog2"):
        v = float(G.linear_to_log(np.array(0.18), prof))
        out = G.evaluate(G.Grade(profile=prof, lut=G.BUILTIN), np.array([[v, v, v]]))[0]
        assert 0.42 < out[0] < 0.55 and np.ptp(out) < 0.01   # neutral grey stays neutral


def test_identity_grade_is_identity():
    g = G.Grade()
    assert g.is_identity()
    x = np.random.default_rng(0).random((50, 3))
    assert np.allclose(G.evaluate(g, x), x, atol=1e-9)
    assert not G.Grade(exposure=0.5).is_identity() and not G.Grade(profile="slog3").is_identity()


def test_sliders_move_in_the_right_direction():
    x = np.array([[0.15, 0.15, 0.15], [0.5, 0.5, 0.5], [0.85, 0.85, 0.85], [0.6, 0.4, 0.3]])
    base = G.evaluate(G.Grade(), x)
    c = G.evaluate(G.Grade(contrast=0.5), x)
    assert c[0, 0] < base[0, 0] and c[2, 0] > base[2, 0]
    assert G.evaluate(G.Grade(shadows=1), x)[0, 0] > base[0, 0]
    assert G.evaluate(G.Grade(highlights=-1), x)[2, 0] < base[2, 0]
    s = G.evaluate(G.Grade(saturation=0), x)[3]
    assert np.ptp(s) < 1e-6
    w = G.evaluate(G.Grade(temperature=1), x)[1]
    assert w[0] > w[2]


def test_cube_roundtrip_and_strength(tmp_path, invert_lut):
    c = G.read_cube(invert_lut)
    assert c.is_3d and c.table.shape == (17, 17, 17, 3)
    x = np.array([[0.2, 0.5, 0.9]])
    assert np.allclose(G.apply_cube(c, x), 1 - x, atol=1e-4)
    half = G.evaluate(G.Grade(lut=str(invert_lut), strength=0.5), x)
    assert np.allclose(half, 0.5, atol=1e-3)


def test_cube_with_header_variants(tmp_path):
    p = tmp_path / "odd.cube"
    p.write_text('# made by a camera\nTITLE "Odd"\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\nLUT_3D_SIZE 2\n'
                 + "\n".join("{} {} {}".format(r, g, b) for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n")
    c = G.read_cube(p)
    assert c.title == "Odd" and np.allclose(G.apply_cube(c, np.array([[0.3, 0.6, 0.1]])), [[0.3, 0.6, 0.1]])
    bad = tmp_path / "bad.cube"
    bad.write_text("hello")
    with pytest.raises(Exception, match="cube"):
        G.read_cube(bad)


def test_lut_file_for_ffmpeg_is_cached(warm_lut):
    g = G.Grade(lut=str(warm_lut), exposure=0.3)
    f1, d = G.lut3d_filter(g)
    f2, _ = G.lut3d_filter(g)
    assert f1 == f2 and (d / f1.split("=")[2].split(":")[0]).exists()


# ---------------------------------------------------------------- presets & recent LUTs
def test_presets_and_recent(warm_lut, invert_lut):
    g = G.Grade(profile="slog3cine", lut=str(warm_lut), exposure=0.7, contrast=0.2)
    e = G.save_preset("Interview FX3", g)
    G.save_preset("interview fx3", G.Grade(profile="flog2"))   # same name replaces
    ps = G.presets()
    assert len(ps) == 1 and G.Grade.from_dict(ps[0]["grade"]).profile == "flog2"
    G.save_preset("Outdoor", g)
    assert [p["name"] for p in G.presets()] == ["Outdoor", "interview fx3"]
    G.delete_preset(G.presets()[0]["id"])
    assert [p["name"] for p in G.presets()] == ["interview fx3"]
    G.remember_lut(str(invert_lut))
    assert G.recent_luts()[0] == str(invert_lut.resolve()) and str(warm_lut.resolve()) in G.recent_luts()
    G.remember_last(g)
    assert G.last_grade() == g


# ---------------------------------------------------------------- FFmpeg side
@pytest.fixture(scope="module")
def grey_clip(tmp_path_factory):
    """Flat grey clip with sound, built from exact raw YUV (Y = 41 % ≈ 18 % grey in S-Log3, U = V = 128)
    and encoded losslessly, so no FFmpeg version can tint it."""
    if FF is None:
        pytest.skip("FFmpeg not available")
    d = tmp_path_factory.mktemp("g")
    p = d / "C0001.MP4"
    y = int(round(16 + 0.4106 * 219))
    w, h, n = 1920, 1080, 75
    frame = bytes([y]) * (w * h) + bytes([128]) * (w * h // 2)
    proc = subprocess.Popen([FF, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{w}x{h}",
                             "-r", "25", "-i", "-", "-f", "lavfi", "-i", "sine=d=3", "-map", "0:v", "-map", "1:a",
                             "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
                             "-c:a", "aac", "-shortest", str(p)], stdin=subprocess.PIPE)
    for _ in range(n):
        proc.stdin.write(frame)
    proc.stdin.close()
    assert proc.wait() == 0
    return p


def center(path):
    return np.array(Image.open(path).convert("RGB").getpixel((960, 540)))


def test_graded_screenshot(grey_clip, invert_lut):
    plain = framegrab.save_frame(grey_clip, 1.0, "PNG")
    inv = framegrab.save_frame(grey_clip, 1.0, "PNG", grade=G.Grade(lut=str(invert_lut)))
    a, b = center(plain), center(inv)
    assert abs(int(a[0]) + int(b[0]) - 255) <= 3
    conv = framegrab.save_frame(grey_clip, 1.0, "JPG", grade=G.Grade(profile="slog3cine", lut=G.BUILTIN))
    c = center(conv)
    assert 105 < c[0] < 140 and np.ptp(c) <= 4          # ~48 % neutral grey after conversion
    plain_jpg = framegrab.save_frame(grey_clip, 1.0, "JPG")
    assert np.ptp(center(plain_jpg)) <= 3 and abs(int(center(plain_jpg)[0]) - int(a[0])) <= 3   # no colour cast


def test_export_graded(grey_clip, warm_lut):
    g = G.Grade(lut=str(warm_lut), exposure=0.5)
    out = framegrab.export_graded(grey_clip, g, "H264")[0]
    assert out.name == "C0001 graded.mp4"
    i = media.probe(out)
    assert i.vcodec == "h264" and i.has_audio and "bt709" in i.raw and abs(i.duration - 3) < 0.2
    shot = framegrab.save_frame(out, 1.0, "PNG")
    r, g_, b = center(shot)
    assert r > b + 15                                   # the warm LUT made it into the file
    hevc = framegrab.export_graded(grey_clip, g, "HEVC")[0]
    assert media.probe(hevc).vcodec == "hevc" and media.probe(hevc).ten_bit
    pr = framegrab.export_graded(grey_clip, g, "PRORES")[0]
    assert media.probe(pr).vcodec == "prores" and pr.suffix == ".mov"


def test_atempo_chain():
    assert atempo_chain(1.5) == "atempo=1.5000"
    assert atempo_chain(4) == "atempo=2.0,atempo=2.0000"


# ---------------------------------------------------------------- player
def settle(app, cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.02)
    return cond()


def test_player_grading_presets_and_clips(qapp, grey_clip, invert_lut, tmp_path):
    other = tmp_path / "C0002.MP4"
    other.write_bytes(grey_clip.read_bytes())
    w = framegrab.make_window([str(grey_clip), str(other)], None)
    assert settle(qapp, lambda: w.view.img is not None)
    g0 = w.view.img.pixelColor(100, 100).red()
    w.use_lut(str(invert_lut))
    assert settle(qapp, lambda: abs(w.view.img.pixelColor(100, 100).red() - (255 - g0)) <= 4)
    assert w.lut_box.currentText() == "Invert"
    w.rows["exposure"].s.setValue(10)            # +1 EV, live
    w._slider_done()
    assert w.grade.exposure == 1.0
    # preset carries LUT + sliders to the next clip
    G.save_preset("Inverted +1", w.grade)
    w._fill_presets()
    w.open_clip(1)
    assert w.clip_lab.text() == "2 / 2" and w.grade.lut == str(invert_lut.resolve())
    w.rows["exposure"].s.setValue(0)
    w._slider_done()
    w.preset_box.setCurrentIndex(1)
    w._preset_chosen(1)
    assert w.grade.exposure == 1.0
    # graded screenshot + export of both clips (each with its own grade)
    w.screenshot()
    assert settle(qapp, lambda: len(w.saved) == 1)
    w.export(all_clips=True)
    assert len(w.last_export) == 2 and all(p.exists() for p in w.last_export)
    # compare shows the original
    w._set_compare(True)
    assert settle(qapp, lambda: abs(w.view.img.pixelColor(100, 100).red() - g0) <= 4)
    w._set_compare(False)
    w.close()
    assert G.last_grade().lut == str(invert_lut.resolve())


def test_playback_is_smooth(qapp, tmp_path):
    """1× playback of a 25 fps clip should show close to 25 frames per second."""
    p = tmp_path / "smooth.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=25:d=6",
                    "-c:v", "libx264", "-preset", "ultrafast", str(p)], check=True)
    w = framegrab.make_window([str(p)], None)
    assert settle(qapp, lambda: w.view.img is not None)
    shown = []
    w.source.frame.connect(lambda img, t: shown.append(time.monotonic()))
    w.toggle_play()
    settle(qapp, lambda: False, timeout=3.5)
    w.toggle_play()
    w.close()
    span = [t for t in shown if t - shown[0] > 0.5]        # skip the start-up second
    fps = (len(span) - 1) / (span[-1] - span[0]) if len(span) > 2 else 0
    assert fps > 18, fps


def test_audio_drives_the_clock(monkeypatch, grey_clip):
    """With a (simulated) sound card the player hears real PCM and the video follows its clock."""
    from dragshift import audioplay
    got = []

    class FakeStream:
        latency = 0.02

        def __init__(self, samplerate, channels, dtype, callback, **kw):
            self.cb, self.run = callback, False

        def start(self):
            import threading
            self.run = True

            def loop():
                buf = bytearray(480 * 4)
                while self.run:
                    self.cb(buf, 480, None, None)
                    got.append(bytes(buf))
                    time.sleep(0.01)
            threading.Thread(target=loop, daemon=True).start()

        def abort(self):
            self.run = False

        def close(self):
            pass

    class FakeSD:
        RawOutputStream = FakeStream
    monkeypatch.setattr(audioplay, "sd", FakeSD)
    monkeypatch.setattr(audioplay, "SOUND_OK", True)
    a = audioplay.AudioPlayer()
    assert a.start(grey_clip, 1.0, 1.0, media.probe(grey_clip))
    time.sleep(0.6)
    pos = a.position()
    a.stop()
    assert 1.2 < pos < 1.8, pos
    assert any(any(b) for b in got)                  # real (non-silent) samples reached the card
    assert not a.start(grey_clip, 0, 8.0, media.probe(grey_clip))   # silent above 4×
