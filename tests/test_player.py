"""1.6: one player window with a file list, trimming (video + audio), typed values, start menu."""
import subprocess
import time
from pathlib import Path

import pytest
from PIL import Image

from dragshift import framegrab, media

try:
    FF = media.ffmpeg_exe()
except Exception:  # pragma: no cover
    FF = None
pytestmark = pytest.mark.skipif(FF is None, reason="FFmpeg not available")


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def ff(*a):
    subprocess.run([FF, "-y", "-loglevel", "error", *a], check=True)


@pytest.fixture
def shoot(tmp_path):
    """A folder like a card dump: 3 clips (one in a subfolder), a WAV and a stray text file."""
    d = tmp_path / "Shoot"
    (d / "B-Roll").mkdir(parents=True)
    for i, name in enumerate(["C0001.MP4", "C0002.MP4", "B-Roll/C0003.MP4"]):
        ff("-f", "lavfi", "-i", f"testsrc2=s=640x360:r=25:d={2 + i}", "-f", "lavfi", "-i", f"sine=d={2 + i}",
           "-c:v", "libx264", "-g", "25", "-c:a", "aac", str(d / name))
    ff("-f", "lavfi", "-i", "sine=f=440:d=4", "-ac", "2", str(d / "Voice.wav"))
    (d / "notes.txt").write_text("x")
    return d


def settle(app, cond, timeout=20):
    end = time.time() + timeout
    while time.time() < end and not cond():
        app.processEvents()
        time.sleep(0.02)
    return cond()


def test_expand_media(shoot):
    got = [p.name for p in framegrab.expand_media([shoot])]
    assert got == ["C0001.MP4", "C0002.MP4", "Voice.wav", "C0003.MP4"]
    assert framegrab.expand_media([shoot / "C0001.MP4", shoot / "C0001.MP4"]) == [shoot / "C0001.MP4"]


def test_folder_list_thumbnails_and_drop(qapp, shoot):
    w = framegrab.make_window([str(shoot)], None)
    assert len(w.paths) == 4 and w.list.count() == 4 and w.idx == 0
    # thumbnails + duration arrive in the background
    assert settle(qapp, lambda: "0:02" in w.list.item(0).text() and "640×360" in w.list.item(0).text())
    assert settle(qapp, lambda: "audio" in w.list.item(2).text())
    w.list.setCurrentRow(1)
    assert w.idx == 1 and w.clip_lab.text() == "2 / 4"
    # dropping more files into the open player adds them (no duplicates)
    extra = shoot / "Extra.mp4"
    ff("-f", "lavfi", "-i", "testsrc2=s=320x240:r=25:d=1", "-c:v", "libx264", str(extra))
    assert len(w.add_paths([str(extra), str(shoot / "C0001.MP4")])) == 1
    assert len(w.paths) == 5 and w.idx == 4
    # audio in the list switches to the waveform view
    w.open_clip(2)
    assert w.audio_mode and settle(qapp, lambda: w.view.img is not None)
    w.close()


def test_empty_player(qapp):
    w = framegrab.make_window([], None)
    assert w.paths == [] and "Drop videos" in w.view.message
    w.toggle_play()                     # nothing loaded: no crash
    w.close()


def test_trim_range_loop_and_exports(qapp, shoot):
    w = framegrab.make_window([str(shoot / "C0002.MP4")], None)
    assert settle(qapp, lambda: w.view.img is not None)
    w.goto(10)
    w.set_in()
    w.goto(59)
    w.set_out()
    a, b = w.range_seconds()
    assert (round(a, 3), round(b, 3)) == (0.4, 2.4) and "2.00 s" in w.range_lab.text()
    assert (w.slider.tin, w.slider.tout) == (10, 59)
    # loop: reaching Out jumps back to In
    w.loop_btn.setChecked(True)
    w.playing = True
    w.n = 59
    assert w._check_range() and w.playing
    w.pause()
    # stop at Out when not looping
    w.loop_btn.setChecked(False)
    w.stop_at_out = True
    w.playing = True
    w.n = 60
    assert w._check_range() and not w.playing and w.n == 59
    # exact trim (re-encode) and fast trim (copy)
    w.trim_mode.setCurrentIndex(0)
    w.save_trim()
    exact = w.last_export[0]
    assert exact.name == "C0002 trim.mp4" and abs(media.probe(exact).duration - 2.0) < 0.1
    w.trim_mode.setCurrentIndex(1)
    w.save_trim()
    fast = w.last_export[0]
    assert media.probe(fast).vcodec == "h264" and 1.5 < media.probe(fast).duration < 3.2
    w.clear_range()
    assert w.range_seconds() is None and not w.trim_btn.isEnabled()
    w.close()


def test_audio_trim_and_playback(qapp, shoot):
    w = framegrab.make_window([str(shoot / "Voice.wav")], None, "trim")
    assert w.audio_mode and w.frames == 400 and w.trim_mode.currentData() == "audio"
    w.goto(50)
    w.set_in()
    w.goto(175)
    w.set_out()
    w.save_trim()
    out = w.last_export[0]
    i = media.probe(out)
    assert out.name == "Voice trim.wav" and i.acodec.startswith("pcm") and abs(i.duration - 1.25) < 0.02
    # playback moves the playhead even without a sound card
    w.goto(0)
    w.toggle_play()
    assert settle(qapp, lambda: w.n > 30, timeout=5)
    w.toggle_play()
    assert not w.playing
    w.close()


def test_mp3_trim_stays_mp3(tmp_path):
    p = tmp_path / "song.mp3"
    ff("-f", "lavfi", "-i", "sine=d=5", "-c:a", "libmp3lame", "-b:a", "192k", str(p))
    out = framegrab.trim_audio(p, 1.0, 3.5)[0]
    assert out.suffix == ".mp3" and abs(media.probe(out).duration - 2.5) < 0.1


def test_typed_values(qapp, shoot):
    w = framegrab.make_window([str(shoot / "C0001.MP4")], None)
    row = w.rows["exposure"]
    row.box.setText("1,3")                      # typing 1,3 + Enter (German comma works)
    row.box.editingFinished.emit()
    assert row.s.value() == 13 and w.grade.exposure == 1.3 and row.box.text() == "+1.3 EV"
    row.box.setText("nonsense")
    row.box.editingFinished.emit()
    assert row.box.text() == "+1.3 EV"
    row.box.setText("99")                       # clamped to the slider range
    row.box.editingFinished.emit()
    assert w.grade.exposure == 4.0
    w.rows["contrast"].box.setValue(-25)
    assert w.grade.contrast == -0.25
    w.rows["saturation"].s.setValue(150)        # slider → box follows
    assert w.rows["saturation"].box.value() == 150
    w.close()


def test_start_menu_and_player_reuse(qapp, shoot, monkeypatch):
    from dragshift.app import DragShiftApp
    items = DragShiftApp.START_ITEMS
    assert [k for _, _, k, _ in items][:2] == ["start:player", "start:trim"]

    class Fake:
        windows = []
        notify = None
        toasts = None

        def open_window(self, w):
            self.windows.append(w)
            w.show()
    f = Fake()
    w1 = DragShiftApp.open_player(f, [str(shoot / "C0001.MP4")])
    w2 = DragShiftApp.open_player(f, [str(shoot / "C0002.MP4")])
    assert w1 is w2 and len(f.windows) == 1 and len(w1.paths) == 2 and w1.idx == 1
    DragShiftApp.open_player(f, [str(shoot / "C0001.MP4")])     # already listed: switch to it
    assert w1.idx == 0
    w1.close()


def test_folder_ring_offers_player(qapp, shoot):
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, [str(shoot)])
    keys = [i.key for i in rings["Folder"]]
    assert keys == ["bulk:all", "player:player", "player:trim"]
