"""1.8: virus scan, folder icons, screenshots + editor, screen recording, shortcuts."""
import json
import sys
from pathlib import Path

import pytest
from PIL import Image
from PySide6.QtCore import QPointF, QRect

from dragshift import capture, foldericons, hotkeys, recorder, snapeditor, virusscan
from dragshift.tools import Shape


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- virus scan
def test_program_category_and_ring():
    from dragshift.app import DragShiftApp
    from dragshift.util import category
    assert category("setup.EXE") == "program" and category("a.msi") == "program" and category("x.zip") == "program"
    assert DragShiftApp.build_rings(None, ["C:/x/setup.exe"]) == {"Check": [
        DragShiftApp.build_rings(None, ["C:/x/setup.exe"])["Check"][0]]}
    assert DragShiftApp.build_rings(None, ["C:/x/setup.exe"])["Check"][0].key == "scan:scan"


def test_defender_output_parsing():
    assert virusscan.parse_defender(0, "Scanning C:\\a.exe found no threats.").state == "clean"
    c = virusscan.parse_defender(2, "Threat                  : Trojan:Win32/Wacatac.B!ml\nResources: 1 total")
    assert c.state == "threat" and "Wacatac" in c.text
    assert virusscan.parse_defender(2, "").state == "threat"
    assert virusscan.parse_defender(5, "boom").state == "error"


def _vt_payload(mal, sus, und, label=""):
    res = {f"E{i}": {"category": "malicious", "result": f"Bad.{i}"} for i in range(mal)}
    res.update({f"S{i}": {"category": "suspicious", "result": None} for i in range(sus)})
    res.update({f"U{i}": {"category": "undetected", "result": None} for i in range(und)})
    return {"data": {"attributes": {
        "last_analysis_stats": {"malicious": mal, "suspicious": sus, "undetected": und, "harmless": 0},
        "last_analysis_results": res, "last_analysis_date": 0,
        "popular_threat_classification": {"suggested_threat_label": label} if label else {}}}}


def test_virustotal_parsing():
    clean = virusscan.parse_vt(_vt_payload(0, 0, 70))
    assert clean.state == "clean" and "0 of 70" in clean.text
    one = virusscan.parse_vt(_vt_payload(1, 0, 69))
    assert one.state == "suspicious" and one.details == ["E0: Bad.0"]
    bad = virusscan.parse_vt(_vt_payload(40, 2, 28, "trojan.agent"))
    assert bad.state == "threat" and "40 of 70" in bad.text and "trojan.agent" in bad.text and len(bad.details) == 42
    assert virusscan.parse_vt({"data": {"attributes": {}}}).state == "pending"


def test_vt_lookup_with_fake_server(monkeypatch):
    calls = []

    def fake(method, url, key, *a, **k):
        calls.append((method, url, key))
        if url.endswith("deadbeef"):
            raise FileNotFoundError
        return _vt_payload(0, 0, 66)
    monkeypatch.setattr(virusscan, "_vt", fake)
    assert virusscan.vt_lookup("abc", "").state == "off" and not calls
    assert virusscan.vt_lookup("abc", "KEY").state == "clean"
    assert virusscan.vt_lookup("deadbeef", "KEY").state == "unknown"
    assert calls[0] == ("GET", "/files/abc", "KEY")


def test_scan_report_and_window(qapp, tmp_path, monkeypatch):
    f = tmp_path / "tool.exe"
    f.write_bytes(b"MZ" + b"\0" * 5000)
    monkeypatch.setattr(virusscan, "_vt", lambda *a, **k: _vt_payload(0, 0, 70))
    monkeypatch.setattr(virusscan, "defender_scan", lambda p: virusscan.Check("clean", "Windows Defender: no threats"))
    steps = []
    r = virusscan.scan(f, "KEY", step=lambda rep: steps.append(rep.vt.state))
    import hashlib
    assert r.sha256 == hashlib.sha256(f.read_bytes()).hexdigest() and len(steps) == 4
    assert r.verdict()[0] == "good"
    r.defender = virusscan.Check("threat", "x")
    assert r.verdict()[0] == "bad"

    class S:
        def get(self, k):
            return "KEY" if k == "vt_key" else ""
    w = virusscan.ScanWindow([str(f), str(f)], S())
    assert len(w.cards) == 1                          # duplicates are ignored
    import time
    t = time.time()
    while time.time() - t < 60 and not w.cards[0].again.isEnabled():
        qapp.processEvents()
        time.sleep(0.02)
    assert "No threats" in w.cards[0].verdict.text()
    w.close()


def test_multipart_body(tmp_path):
    f = tmp_path / "a b.bin"
    f.write_bytes(b"\x00\x01payload")
    body, ctype = virusscan._multipart(f)
    b = ctype.split("boundary=")[1]
    assert body.startswith(f"--{b}\r\n".encode()) and b"\x00\x01payload" in body and body.endswith(f"--{b}--\r\n".encode())
    assert b'filename="a b.bin"' in body


# ---------------------------------------------------------------- folder icons
def test_render_and_ico(qapp, tmp_path):
    for e in foldericons.builtin_icons():
        img = e.image(64)
        assert img.width() == 64 and img.pixelColor(32, 40).alpha() > 200      # the folder body is opaque
    ico = tmp_path / "x.ico"
    foldericons.write_ico(foldericons.render_folder("#3ea6ff", "star"), ico)
    im = Image.open(ico)
    assert (256, 256) in im.ico.sizes() and (16, 16) in im.ico.sizes()


def test_desktop_ini_editing():
    lines = ["[.ShellClassInfo]", "LocalizedResourceName=@shell32.dll,-21770", "IconFile=old.ico", "IconIndex=3",
             "", "[ViewState]", "Mode=", "FolderType=Pictures"]
    out = foldericons.ini_set_icon(lines, ".dragshift-icon-1.ico,0")
    assert "IconResource=.dragshift-icon-1.ico,0" in out and "IconFile=old.ico" not in out
    assert "LocalizedResourceName=@shell32.dll,-21770" in out and "FolderType=Pictures" in out
    assert out.index("IconResource=.dragshift-icon-1.ico,0") < out.index("[ViewState]")
    back = foldericons.ini_set_icon(out, None)
    assert not any(l.startswith("IconResource") for l in back) and not foldericons.ini_is_empty(back)
    assert foldericons.ini_set_icon([], "a.ico,0") == ["[.ShellClassInfo]", "IconResource=a.ico,0"]
    assert foldericons.ini_is_empty(foldericons.ini_set_icon(["[.ShellClassInfo]", "IconResource=a,0"], None))


def test_desktop_ini_utf16_roundtrip(tmp_path):
    ini = tmp_path / "desktop.ini"
    ini.write_bytes(b"\xff\xfe" + "[.ShellClassInfo]\r\nInfoTip=Fotos ä\r\n".encode("utf-16-le"))
    lines, enc = foldericons._read_ini(ini)
    assert enc == "utf-16" and lines[1] == "InfoTip=Fotos ä"
    foldericons._write_ini(ini, foldericons.ini_set_icon(lines, "i.ico,0"), enc)
    assert ini.read_bytes().startswith(b"\xff\xfe") and "IconResource=i.ico,0" in ini.read_bytes().decode("utf-16")


def test_library_add_and_window(qapp, tmp_path, monkeypatch):
    png = tmp_path / "logo.png"
    Image.new("RGBA", (300, 120), (255, 0, 0, 255)).save(png)
    e = foldericons.add_image(png)
    assert e.file.exists() and Image.open(e.file).size == (256, 256)
    ico = tmp_path / "app.ico"
    Image.new("RGBA", (64, 64), (0, 200, 0, 255)).save(ico, sizes=[(16, 16), (64, 64)])
    assert Image.open(foldericons.add_image(ico).file).size == (256, 256)
    c = foldericons.save_custom("#ff5d5d", "", "26")
    assert c.file.name == "26.png" and len(foldericons.custom_icons()) == 3
    applied = []
    monkeypatch.setattr(foldericons, "apply", lambda f, entry: applied.append((Path(f).name, entry.name)))
    d1, d2 = tmp_path / "Urlaub", tmp_path / "Kunden"
    d1.mkdir()
    d2.mkdir()
    w = foldericons.FolderIconWindow([str(d1)])
    assert w.targets == [d1]
    w.set_targets(w.targets + [d2, d1])
    assert w.targets == [d1, d2]
    item = next(w.grid.item(i) for i in range(w.grid.count()) if w.grid.item(i).text() == "Green")
    w.clicked(item)
    assert applied == [("Urlaub", "Green"), ("Kunden", "Green")] and "2 folders" in w.status.text()
    w.close()


@pytest.mark.skipif(sys.platform != "win32", reason="Explorer folder icons")
def test_apply_and_reset_on_windows(qapp, tmp_path):
    d = tmp_path / "Projekt"
    d.mkdir()
    foldericons.apply(d, foldericons.builtin_icons()[3])
    ico = foldericons.current_icon(d)
    assert ico is not None and (d / "desktop.ini").exists()
    assert f"IconResource={ico.name},0" in foldericons._read_ini(d / "desktop.ini")[0]
    foldericons.apply(d, foldericons.builtin_icons()[5])        # replacing leaves a single icon file
    assert len(list(d.glob(".dragshift-icon-*.ico"))) == 1
    foldericons.reset(d)
    assert foldericons.current_icon(d) is None and not (d / "desktop.ini").exists()


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
    for k in ("start:shot", "start:record", "start:scan", "start:icons"):
        assert k in keys


def test_folder_ring_has_icons(tmp_path):
    from dragshift.app import DragShiftApp
    rings = DragShiftApp.build_rings(None, [str(tmp_path)])
    assert "icons:icons" in [i.key for i in rings["Folder"]]
