"""1.8: screenshots + editor, screen recording, shortcuts."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QPointF, QRect

from dragshift import capture, hotkeys, recorder, snapeditor
from dragshift.tools import Shape


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- shortcuts
def test_hotkey_parsing():
    assert hotkeys.win_combo("Ctrl+Shift+1") == (hotkeys.MOD_NOREPEAT | hotkeys.MOD_CONTROL | hotkeys.MOD_SHIFT, 0x31)
    assert hotkeys.win_combo("Alt+F9") == (hotkeys.MOD_NOREPEAT | hotkeys.MOD_ALT, 0x78)
    assert hotkeys.win_combo("Print")[1] == 0x2C
    assert hotkeys.win_combo("") is None


def test_app_registers_shortcuts(qapp, monkeypatch):
    from dragshift.app import DragShiftApp
    from dragshift.settings import DEFAULTS

    class FakeHK:
        supported = True

        def __init__(self):
            self.got = []

        def clear(self):
            self.got = []

        def register(self, seq, cb):
            self.got.append(seq)
            return seq != "Ctrl+Shift+3"

    class FakeSettings:
        def get(self, k):
            return DEFAULTS[k]
    errors = []

    class Toasts:
        def error(self, t, d):
            errors.append(d)
    f = type("F", (), {})()
    f.hotkeys, f.settings, f.toasts, f.HOTKEYS = FakeHK(), FakeSettings(), Toasts(), DragShiftApp.HOTKEYS
    for _, fn, _a in DragShiftApp.HOTKEYS:
        setattr(f, fn, lambda: None)
    taken = DragShiftApp.register_hotkeys(f, warn=True)
    assert f.hotkeys.got == ["Ctrl+Shift+1", "Ctrl+Shift+2", "Ctrl+Shift+3", "Ctrl+Shift+5"]
    assert taken == ["Ctrl+Shift+3"] and "Ctrl+Shift+3" in errors[0]


# ---------------------------------------------------------------- capture helpers
def test_names_and_folders(tmp_path):
    n = capture.format_name("Shot %Y-%m-%d %H.%M.%S %r", when=0)
    assert n.startswith("Shot 19") and len(n.split()[-1]) == 4
    assert capture.format_name("a/b:c*?") == "a-b-c--"
    assert capture.format_name("") .startswith("DragShift ")
    d = capture.capture_dir(str(tmp_path / "caps"))
    assert d.is_dir()
    (d / "x.png").write_bytes(b"")
    assert capture.unique(d, "x", ".png").name == "x (2).png"


def test_physical_rect():
    class Scr:
        def devicePixelRatio(self):
            return 1.5

        def geometry(self):
            return QRect(-1920, 0, 1280, 720)
    r = capture.physical_rect(Scr(), QRect(100, 50, 400, 300))
    if sys.platform == "darwin":
        assert (r.x(), r.y(), r.width(), r.height()) == (150, 75, 600, 450)
    else:
        assert (r.x(), r.y(), r.width(), r.height()) == (-1920 + 150, 75, 600, 450)


def test_picker_and_card(qapp, tmp_path):
    pk = capture.AreaPicker()
    got = {}
    pk.picked.connect(lambda s, r, pm: got.update(img=capture.crop_pixmap(pm, r)))
    pk.start()
    assert pk.shades
    pk.done(pk.shades[0], QRect(10, 10, 120, 80))
    assert not pk.shades and got["img"].width() == round(120 * got["img"].devicePixelRatio())
    stack = capture.CardStack()
    card = capture.CaptureCard(got["img"], "Copied", "", [("Edit", lambda: None)], path_fn=lambda: tmp_path / "a.png")
    stack.add(card)
    assert stack.cards == [card]
    card.close()
    assert stack.cards == []


# ---------------------------------------------------------------- editor
def test_editor_draw_crop_undo_save(qapp, tmp_path):
    from PySide6.QtGui import QColor, QImage
    img = QImage(400, 300, QImage.Format_ARGB32)
    img.fill(QColor("#ffffff"))
    img.setDevicePixelRatio(2)

    class S:
        def get(self, k):
            return {"capture_dir": str(tmp_path), "capture_name": "Test %Y"}.get(k, "")
    w = snapeditor.SnapEditor(img, S())
    v = w.view
    for k, pts in (("Filled", [QPointF(10, 10), QPointF(60, 60)]), ("Arrow", [QPointF(100, 100), QPointF(200, 150)]),
                   ("Spotlight", [QPointF(200, 10), QPointF(300, 90)])):
        w.push()
        v.shapes.append(Shape(k, "#ff3b30", 4, pts))
    out = w.result()
    assert out.size().width() == 400 and out.pixelColor(30, 30).red() > 200 and out.pixelColor(30, 30).green() < 100
    assert out.pixelColor(390, 290).red() < 160          # spotlight dims outside its box
    assert out.pixelColor(250, 50).red() > 240           # …but not inside
    w.apply_crop(QRect(0, 0, 100, 100))
    assert v.image.width() == 100 and v.shapes[1].pts[0] == QPointF(100, 100)
    w.undo()
    assert v.image.width() == 400 and len(v.shapes) == 3
    w.undo()
    assert len(v.shapes) == 2
    w.redo()
    assert len(v.shapes) == 3
    saved = []
    w.saved.connect(saved.append)
    w.done()
    assert saved and saved[0].parent == tmp_path and saved[0].name.startswith("Test ") and saved[0].exists()


def test_pixelate_hides_detail(qapp):
    from PySide6.QtGui import QColor, QImage
    img = QImage(200, 200, QImage.Format_ARGB32)
    for y in range(200):
        for x in range(200):
            img.setPixelColor(x, y, QColor(255, 255, 255) if (x // 2 + y // 2) % 2 else QColor(0, 0, 0))
    out = snapeditor.flatten(img, [Shape("Pixelate", "#000", 4, [QPointF(0, 0), QPointF(200, 200)])])
    vals = {out.pixelColor(x, 50).red() for x in range(40, 60)}
    assert max(vals) - min(vals) < 40                 # the fine checkerboard is gone


# ---------------------------------------------------------------- recording
def test_recorder_args(tmp_path):
    a = recorder.build_args("ffmpeg", QRect(-1920, 10, 801, 601), tmp_path / "o.mp4", 30, "Mic (USB)", True, False)
    assert a[-1].endswith("o.mp4") and "-preset" in a
    if sys.platform == "win32":
        assert a[a.index("-f") + 1] == "gdigrab" and "800x600" in a and "-1920" in a and "audio=Mic (USB)" in a
    elif sys.platform == "darwin":
        assert any(x.startswith("crop=800:600:-1920:10") for x in a)
    else:
        assert "x11grab" in a and "800x600" in a
    g = recorder.build_args("ffmpeg", QRect(0, 0, 100, 100), tmp_path / "o.mp4", 30, "Mic", True, True)
    assert "-an" in g and "audio=Mic" not in g
    gif = recorder.gif_args("ffmpeg", tmp_path / "a.mp4", tmp_path / "a.gif")
    assert "palettegen" in gif[gif.index("-vf") + 1] and gif[-1].endswith("a.gif")


def test_gif_conversion(tmp_path):
    import subprocess
    from dragshift import media
    try:
        ff = media.ffmpeg_exe()
    except Exception:
        pytest.skip("no ffmpeg")
    src = tmp_path / "c.mp4"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=320x240:d=1", str(src)], check=True)
    out = tmp_path / "c.gif"
    subprocess.run(recorder.gif_args(ff, src, out), check=True)
    im = Image.open(out)
    assert im.format == "GIF" and im.n_frames >= 10


def test_start_menu_has_capture_items():
    from dragshift.app import DragShiftApp
    keys = [k for _, _, k, _ in DragShiftApp.START_ITEMS]
    for k in ("start:shot", "start:record"):
        assert k in keys
    assert "start:scan" not in keys and "start:icons" not in keys




def test_only_shift_opens_the_menu():
    """Ctrl/Alt + drag (selection rectangles, copies) must never open DragShift."""
    from dragshift.settings import TRIGGERS, Settings
    from dragshift.trigger import VK_SHIFT, DragShiftWatcher
    assert [k for k, _ in TRIGGERS] == ["shift"]
    s = Settings()
    s.q.setValue("trigger", "ctrl")              # what an older version may have saved
    assert s.get("trigger") == "shift"
    s.set("trigger", "alt")
    assert s.get("trigger") == "shift"
    w = DragShiftWatcher(lambda: False, lambda: False)
    w.set_key("ctrl")
    assert w.vk == VK_SHIFT and w.key_name == "shift"
