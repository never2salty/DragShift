"""Screenshot player: exact frame seeking, full-res saves, playback stream, window controls."""
import subprocess
import time

import pytest
from PIL import Image

from dragshift import framegrab, media, report

try:
    FF = media.ffmpeg_exe()
except Exception:  # pragma: no cover
    FF = None
pytestmark = pytest.mark.skipif(FF is None, reason="FFmpeg not available")


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))   # no saved grade from elsewhere


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.fixture(scope="module")
def clip(tmp_path_factory):
    """4 s at 25 fps; every frame shows its own number in the top-left corner as a grey level."""
    d = tmp_path_factory.mktemp("fg")
    p = d / "Interview.mov"
    # drawbox brightness = frame number * 2 → readable back from the pixel
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=1920x1080:r=25:d=4",
                    "-vf", "format=yuv420p,geq=lum='if(lt(X,200)*lt(Y,200),16+N*2,128)':cb=128:cr=128",
                    "-c:v", "libx264", "-g", "50", "-crf", "1", "-pix_fmt", "yuv420p", str(p)], check=True)
    return p


def lum(path):
    return Image.open(path).convert("L").getpixel((50, 50))


def frame_of(path) -> int:
    """Frame number back from the corner brightness (video luma 16+2N → RGB 2N·255/219)."""
    return round(lum(path) * 219 / 255 / 2)


@pytest.mark.parametrize("n", [0, 1, 37, 63, 99])
def test_exact_frame(clip, n):
    out = framegrab.save_frame(clip, framegrab.frame_time(n, 25), "PNG")
    assert Image.open(out).size == (1920, 1080)
    assert frame_of(out) == n, (n, lum(out))
    out.unlink()


def test_names_and_jpg(clip):
    out = framegrab.save_frame(clip, 2.48, "JPG")
    assert out.name == "Interview 00m02.480s.jpg" and Image.open(out).format == "JPEG"
    again = framegrab.save_frame(clip, 2.48, "JPG")
    assert again != out and again.exists()  # never overwrites


def test_preview_and_fast_seek(clip):
    info = media.probe(clip)
    img = framegrab.grab_preview(clip, info, framegrab.frame_time(30, 25))
    assert img is not None and (img.width(), img.height()) == (1280, 720)
    fast = framegrab.grab_preview(clip, info, 1.9, fast=True)
    assert fast is not None


def test_anamorphic_square_pixels(tmp_path):
    p = tmp_path / "ana.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1440x1080:r=25:d=1",
                    "-vf", "setsar=4/3", "-c:v", "libx264", str(p)], check=True)
    out = framegrab.save_frame(p, 0.2, "PNG")
    assert Image.open(out).size == (1920, 1080)


def test_window_steps_and_screenshot(qapp, clip):
    from PySide6.QtWidgets import QApplication
    got = []
    w = framegrab.make_window([str(clip)], lambda *a: got.append(a))
    assert w.frames == 100 and w.fps == 25

    def settle(cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end and not cond():
            QApplication.processEvents()
            time.sleep(0.02)
        return cond()

    assert settle(lambda: w.view.img is not None)
    w.step(1)
    w.step(1)
    w.step(25)
    assert w.n == 27 and w.slider.value() == 27 and "frame 28 / 100" in w.clock.text()
    w.step(-1000)
    assert w.n == 0
    w.goto(44)
    w.screenshot()
    assert settle(lambda: len(w.saved) == 1)
    assert frame_of(w.saved[0]) == 44 and w.saved[0].name == "Interview 00m01.760s.png"
    # playback moves forward and pauses on an exact frame
    w.speed.setCurrentIndex(2)  # 4×
    w.toggle_play()
    assert settle(lambda: w.n > 60, timeout=15)
    w.toggle_play()
    assert not w.playing
    w.close()
    assert got and got[0][0] == "tool:Screenshots" and len(got[0][2]) == 1
    r = report.summarize("tool:Screenshots", [clip], got[0][2])
    assert r.title == "Screenshot saved" and "1920×1080" in r.detail


def test_play_to_end(qapp, tmp_path):
    from PySide6.QtWidgets import QApplication
    p = tmp_path / "short.mp4"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:r=30:d=1",
                    "-c:v", "libx264", str(p)], check=True)
    w = framegrab.make_window([str(p)], None)
    w.speed.setCurrentIndex(4)
    w.toggle_play()
    end = time.time() + 15
    while time.time() < end and w.playing:
        QApplication.processEvents()
        time.sleep(0.02)
    assert not w.playing and w.n == w.frames - 1
    w.close()


def test_audio_file_opens_as_waveform(qapp, tmp_path):
    p = tmp_path / "a.wav"
    subprocess.run([FF, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1", str(p)], check=True)
    w = framegrab.make_window([str(p)], None)
    assert w.audio_mode and not w.shot_btn.isVisible() and w.frames == 100
    w.close()
