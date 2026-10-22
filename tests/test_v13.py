"""1.3: history, folder conversion, drop icon, 3D/CAD, Adobe files, fit-to-size, updater."""
import gzip
import os
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from dragshift import adobe, bulk, dispatch, history, media, model3d, updater
from dragshift.util import CONFIG, category

CONFIG["gpu_encode"] = False


@pytest.fixture(autouse=True)
def _appdata(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


# ---------------------------------------------------------------- history
def test_history_record_remove_clear(tmp_path):
    from dragshift.report import Result
    out = tmp_path / "a.jpg"
    Image.new("RGB", (8, 8)).save(out)
    for i in range(3):
        history.record(Result(f"PNG → JPG {i}", "a.jpg", [out], [tmp_path / "a.png"]))
    e = history.load()
    assert [x["title"] for x in e] == ["PNG → JPG 2", "PNG → JPG 1", "PNG → JPG 0"]
    history.remove(e[1]["id"])
    assert [x["title"] for x in history.load()] == ["PNG → JPG 2", "PNG → JPG 0"]
    history.clear()
    assert history.load() == []
    assert out.exists()  # clearing history never deletes files


def test_history_window(qapp, tmp_path):
    from dragshift.report import Result
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"x")
    history.record(Result("MOV → MP4", "clip.mp4", [out], [tmp_path / "clip.mov"]))
    history.record(Result("WAV → MP3", "gone.mp3", [tmp_path / "gone.mp3"], [tmp_path / "gone.wav"]))
    w = history.HistoryWindow()
    assert len(w.rows) == 2
    w.search.setText("mp4")
    assert sum(r.isVisibleTo(w) for r in w.rows) == 1
    w._remove(w.rows[0].e["id"])
    assert len(history.load()) == 1
    w._clear_all()                # first click only asks
    assert len(history.load()) == 1
    w._clear_all()
    assert history.load() == []
    w.close()


# ---------------------------------------------------------------- drop icon + routing
def test_drop_icon_emits(qapp, tmp_path):
    from PySide6.QtCore import QMimeData, QUrl
    from dragshift.dropzone import DropIcon
    ic = DropIcon()
    got = []
    ic.dropped.connect(got.append)
    md = QMimeData()
    f = tmp_path / "x.png"
    f.write_bytes(b"")
    md.setUrls([QUrl.fromLocalFile(str(f))])
    assert DropIcon._paths(md) == [str(f)]
    ic.close()


def test_needs_bulk(tmp_path):
    from dragshift.app import needs_bulk
    (tmp_path / "a.png").write_bytes(b"")
    (tmp_path / "b.mp3").write_bytes(b"")
    assert needs_bulk([str(tmp_path)])
    assert needs_bulk([str(tmp_path / "a.png"), str(tmp_path / "b.mp3")])
    assert not needs_bulk([str(tmp_path / "a.png")])


# ---------------------------------------------------------------- folder conversion
@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "Shoot"
    (d / "day2").mkdir(parents=True)
    for i in range(3):
        Image.new("RGB", (64, 48), (i * 60, 90, 200)).save(d / f"img{i}.png")
    Image.new("RGB", (64, 48)).save(d / "day2" / "deep.webp")
    Image.new("RGB", (64, 48)).save(d / "keep.jpg")
    (d / "notes.xyz").write_text("ignored")
    ff = media.ffmpeg_exe()
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=d=1", str(d / "voice.wav")], check=True)
    return d


def test_bulk_scan_defaults(folder, qapp):
    groups, other = bulk.scan([folder], recursive=True)
    assert set(groups) == {".png", ".webp", ".jpg", ".wav"} and other == 1
    assert len(groups[".png"]) == 3
    w = bulk.BulkWindow([str(folder)], lambda *a, **k: None)
    jobs = w.jobs()
    assert {(j[0].suffix, j[2]) for j in jobs} == {(".png", "JPG"), (".webp", "JPG"), (".wav", "MP3")}
    w.recursive.setChecked(False)
    assert ".webp" not in w.groups
    w.close()


def test_bulk_convert_mirror(folder):
    jobs = [(p, category(p), dispatch.default_target(category(p), p.suffix))
            for g in bulk.scan([folder], True)[0].values() for p in g]
    jobs = [j for j in jobs if j[2]]
    root = folder.parent / "Shoot (converted)"

    def dest(src):
        return root / src.parent.relative_to(folder)
    outs, errors = bulk.convert_all(jobs, dest)
    assert not errors
    assert (root / "img0.jpg").exists() and (root / "day2" / "deep.jpg").exists() and (root / "voice.mp3").exists()
    assert not list(folder.glob("*.mp3"))  # originals' folder untouched
    # a converted folder is skipped on the next scan
    groups, _ = bulk.scan([folder.parent], True)
    assert all("(converted)" not in str(p) for g in groups.values() for p in g)


def test_bulk_keeps_going_on_errors(tmp_path):
    bad = tmp_path / "broken.png"
    bad.write_bytes(b"not an image")
    good = tmp_path / "ok.png"
    Image.new("RGB", (4, 4)).save(good)
    outs, errors = bulk.convert_all([(bad, "image", "JPG"), (good, "image", "JPG")])
    assert len(outs) == 1 and len(errors) == 1 and "broken.png" in errors[0]


# ---------------------------------------------------------------- 3D / CAD
@pytest.fixture
def box(tmp_path):
    import trimesh
    p = tmp_path / "Bracket.stl"
    trimesh.creation.box((40, 20, 10)).export(p)
    return p


@pytest.mark.parametrize("target", model3d.targets_for("model", ".stl"))
def test_model_targets(box, target):
    out = model3d.convert(box, "model", target)[0]
    assert out.suffix == model3d.EXT[target]
    if target == "PNG":
        im = Image.open(out)
        assert im.size == (1200, 1200) and len(im.convert("L").getcolors(65536) or []) > 5
    else:
        m = model3d.load_mesh(out)
        assert [round(x) for x in sorted(m.extents)] == [10, 20, 40]


def test_model_roundtrip_3mf(box):
    tmf = model3d.convert(box, "model", "3MF")[0]
    stl = model3d.convert(tmf, "model", "STL")[0]
    assert [round(x) for x in sorted(model3d.load_mesh(stl).extents)] == [10, 20, 40]
    assert category(tmf) == "model" and category("x.step") == "model" and category("x.scad") == "model"


def test_model_tools(box):
    c = model3d.tool(box, "Center")[0]
    b = model3d.load_mesh(c).bounds
    assert abs(b[0][2]) < 1e-6 and abs(b[0][0] + b[1][0]) < 1e-6
    r = model3d.tool(box, "Repair")[0]
    assert model3d.load_mesh(r).is_watertight
    with pytest.raises(Exception, match="nothing to simplify"):
        model3d.tool(box, "Simplify")
    import trimesh
    sphere = box.with_name("Ball.stl")
    trimesh.creation.icosphere(subdivisions=5).export(sphere)
    s = model3d.tool(sphere, "Simplify")[0]
    assert len(model3d.load_mesh(s).faces) < len(model3d.load_mesh(sphere).faces) * 0.6


def test_openscad(tmp_path):
    try:
        model3d.openscad_exe()
    except Exception:
        pytest.skip("OpenSCAD not installed")
    scad = tmp_path / "part.scad"
    scad.write_text("difference(){ cube([30,20,10]); translate([15,10,-1]) cylinder(h=12,r=4,$fn=32); }")
    out = model3d.convert(scad, "model", "STL")[0]
    assert [round(x) for x in sorted(model3d.load_mesh(out).extents)] == [10, 20, 30]
    png = model3d.convert(scad, "model", "PNG")[0]
    assert png.exists()
    bad = tmp_path / "bad.scad"
    bad.write_text("cube([1,2,3]")
    with pytest.raises(Exception, match="OpenSCAD"):
        model3d.convert(bad, "model", "STL")


@pytest.mark.parametrize("target", model3d.DXF_TARGETS)
def test_dxf(tmp_path, target):
    import ezdxf
    p = tmp_path / "plate.dxf"
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 60), (0, 60)], close=True)
    msp.add_circle((50, 30), 12)
    doc.saveas(p)
    out = model3d.convert(p, "dxf", target)[0]
    assert out.suffix == "." + target.lower() and out.stat().st_size > 300


# ---------------------------------------------------------------- Adobe
def test_premiere_for_older(tmp_path):
    xml = (b'<?xml version="1.0" encoding="UTF-8" ?>\n<PremiereData Version="3">\n'
           b'  <Project ObjectRef="1"/>\n  <Project ObjectID="1" ClassID="62ad66dd" Version="43">\n'
           b'    <Node Version="1"/>\n  </Project>\n</PremiereData>\n')
    src = tmp_path / "Imagefilm.prproj"
    src.write_bytes(gzip.compress(xml))
    assert adobe.premiere_version(src) == 43
    out = dispatch.convert_file(src, "premiere", "Older Premiere")[0]
    assert out.name == "Imagefilm for older Premiere.prproj"
    data = gzip.decompress(out.read_bytes())
    assert b'ClassID="62ad66dd" Version="1"' in data and b'<Node Version="1"/>' in data
    assert adobe.premiere_version(src) == 43  # original untouched
    with pytest.raises(Exception, match="already"):
        adobe.premiere_for_older(out)


def test_illustrator_ai(tmp_path):
    from dragshift.convert import fitz
    doc = fitz.open()
    for i in range(2):
        pg = doc.new_page(width=300, height=200)
        pg.insert_text((40, 100), f"Artboard {i + 1}", fontsize=28)
    src = tmp_path / "Logo.ai"
    doc.save(src)
    assert category(src) == "ai"
    for t in adobe.AI_TARGETS:
        outs = dispatch.convert_file(src, "ai", t)
        assert len(outs) == (1 if t == "PDF" else 2)
    junk = tmp_path / "old.ai"
    junk.write_bytes(b"%!PS-Adobe-3.0 EPSF\n")
    with pytest.raises(Exception, match="PDF Compatible"):
        adobe.ai_to(junk, "PNG")


# ---------------------------------------------------------------- fit to size
def test_fit_size(tmp_path):
    ff = media.ffmpeg_exe()
    src = tmp_path / "clip.mov"
    subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=6",
                    "-f", "lavfi", "-i", "sine=d=6", "-c:v", "libx264", "-crf", "8", "-c:a", "aac", str(src)],
                   check=True)
    target = 900_000   # ~1.1 Mbit/s for 6 s → must drop to 720p
    out = media.fit_size(src, target)[0]
    size = out.stat().st_size
    assert size < target and size > target * 0.6, size
    i = media.probe(out)
    assert i.vcodec == "h264" and i.has_audio and i.display_size == (1280, 720)  # scaled to fit the bitrate
    with pytest.raises(Exception, match="too small"):
        media.fit_size(src, 20_000)


def test_compress_custom_target(qapp, tmp_path):
    from dragshift import tools
    p = tmp_path / "photo.png"
    import numpy as np
    Image.fromarray((np.random.default_rng(1).random((900, 1200, 3)) * 255).astype("uint8")).save(p)
    w = tools.CompressWindow([str(p)], lambda *a: None)
    w.mode.button(1).click()
    w.custom_kb.setValue(150)
    assert w._limit() == 150_000
    w.close()


# ---------------------------------------------------------------- updater
def test_version_compare():
    assert updater.newer("1.3.1", "1.3.0") and updater.newer("v2.0", "1.9.9")
    assert not updater.newer("1.3.0", "1.3.0") and not updater.newer("1.2.9", "1.3.0")


def test_latest_parses_release(monkeypatch):
    import io
    import json

    class R(io.BytesIO):
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass
    body = {"tag_name": "v9.9.0", "body": "notes", "html_url": "https://x",
            "assets": [{"name": "DragShift-Setup.exe", "browser_download_url": "https://x/s.exe"},
                       {"name": "DragShift.exe", "browser_download_url": "https://x/p.exe"}]}
    monkeypatch.setattr(updater, "_get", lambda url, timeout=15: R(json.dumps(body).encode()))
    rel = updater.latest()
    assert rel.version == "9.9.0" and rel.setup_url.endswith("s.exe") and rel.portable_url.endswith("p.exe")


def test_latest_private_repo_message(monkeypatch):
    import urllib.error

    def boom(url, timeout=15):
        raise urllib.error.HTTPError(url, 404, "nf", {}, None)
    monkeypatch.setattr(updater, "_get", boom)
    with pytest.raises(Exception, match="public"):
        updater.latest()


def test_settings_and_update_windows(qapp):
    from dragshift.settings import Settings, SettingsWindow, UpdateWindow
    SettingsWindow(Settings(), check_updates=lambda: None).close()
    rel = updater.Release("9.0", "**New**", "https://x/s.exe", None, "https://x")
    UpdateWindow(rel, "1.3.0").close()
