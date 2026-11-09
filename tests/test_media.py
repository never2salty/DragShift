"""Video, audio and camera-RAW conversions. Needs FFmpeg (bundled, on PATH or via imageio-ffmpeg)."""
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from dragshift import convert, media, report
from dragshift.util import CONFIG, category

try:
    FF = media.ffmpeg_exe()
except Exception:  # pragma: no cover
    FF = None
pytestmark = pytest.mark.skipif(FF is None, reason="FFmpeg not available")

CONFIG["gpu_encode"] = False  # deterministic on CI machines without a GPU


def ff(*args):
    subprocess.run([FF, "-y", "-hide_banner", "-loglevel", "error", *args], check=True)


@pytest.fixture(scope="module")
def clips(tmp_path_factory):
    d = tmp_path_factory.mktemp("media")
    prores = d / "Clip A.mov"
    ff("-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=2", "-f", "lavfi", "-i", "sine=d=2:sample_rate=48000",
       "-c:v", "prores_ks", "-profile:v", "1", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s24le", str(prores))
    phone = d / "Phone.mov"
    ff("-f", "lavfi", "-i", "testsrc2=s=1080x1920:r=30:d=2", "-f", "lavfi", "-i", "sine=d=2",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(phone))
    song = d / "Song.flac"
    ff("-f", "lavfi", "-i", "sine=f=330:d=3:sample_rate=96000", "-af", "volume=-20dB,apad=pad_dur=1", "-ac", "2",
       "-c:a", "flac", "-sample_fmt", "s32", str(song))
    return {"prores": prores, "phone": phone, "song": song}


def streams(path):
    return media.probe(path)


def test_categories():
    assert category("a.MOV") == "video" and category("b.mxf") == "video" and category("c.braw") == "video"
    assert category("a.flac") == "audio" and category("b.WAV") == "audio"
    assert category("a.CR3") == "image" and category("b.nef") == "image" and category("c.HEIC") == "image"


def test_probe(clips):
    i = media.probe(clips["prores"])
    assert (i.vcodec, i.width, i.height, i.acodec, i.abits) == ("prores", 1280, 720, "pcm_s24le", 24)
    assert i.ten_bit and abs(i.duration - 2) < 0.1
    a = media.probe(clips["song"])
    assert a.vcodec is None and a.acodec == "flac" and a.abits == 24 and a.sample_rate == 96000


EXPECT_VIDEO = {"MP4": ("h264", ".mp4"), "HEVC": ("hevc", ".mp4"), "PRORES": ("prores", ".mov"),
                "MKV": ("prores", ".mkv"), "WEBM": ("vp9", ".webm"), "GIF": ("gif", ".gif"),
                "MP3": (None, ".mp3"), "WAV": (None, ".wav")}


@pytest.mark.parametrize("target", media.targets_for("video", ".mov"))
def test_video_targets(clips, target):
    out = media.convert(clips["prores"], "video", target)[0]
    codec, ext = EXPECT_VIDEO[target]
    assert out.suffix == ext and out.stat().st_size > 0
    i = streams(out)
    assert i.vcodec == codec
    if target not in ("GIF",):
        assert i.has_audio
    if target == "MP4":
        assert i.pix_fmt == "yuv420p"  # 8-bit H.264 plays everywhere


def test_mp4_rewrap_is_lossless(clips):
    """An H.264 MOV becomes MP4 without re-encoding."""
    t = time.time()
    out = media.convert(clips["phone"], "video", "MP4")[0]
    assert streams(out).vcodec == "h264"
    assert abs(out.stat().st_size - clips["phone"].stat().st_size) < 20_000
    assert time.time() - t < 10


EXPECT_AUDIO = {"MP3": "mp3", "WAV": "pcm_s24le", "FLAC": "flac", "M4A": "aac", "ALAC": "alac",
                "OGG": "vorbis", "OPUS": "opus", "AIFF": "pcm_s24be"}


@pytest.mark.parametrize("target", media.targets_for("audio", ".flac"))
def test_audio_targets(clips, target):
    out = media.convert(clips["song"], "audio", target)[0]
    i = streams(out)
    assert i.acodec == EXPECT_AUDIO[target] and abs(i.duration - 4) < 0.2


def test_audio_from_mp3_targets(clips):
    mp3 = media.convert(clips["song"], "audio", "MP3")[0]
    assert "MP3" not in media.targets_for("audio", ".mp3")
    wav = media.convert(mp3, "audio", "WAV")[0]
    assert streams(wav).acodec == "pcm_s16le"  # lossy source → 16-bit is plenty


@pytest.mark.parametrize("name", [n for n in media.VIDEO_TOOLS if n not in ("Screenshots", "Trim")] + ["Thumbnail"])
def test_video_tools(clips, name):
    if name == "1080p":
        with pytest.raises(Exception, match="already"):
            media.tool(clips["phone"], name)
        return
    out = media.tool(clips["phone"], name)[0]
    assert out.exists() and out.stat().st_size > 0
    i = streams(out)
    if name == "720p":
        assert i.display_size == (720, 1280)
    if name == "Rotate":
        assert (i.width, i.height) == (1920, 1080)
    if name == "Mute":
        assert not i.has_audio and i.has_video
    if name == "Thumbnail":
        assert Image.open(out).size == (1080, 1920)


def test_downscale_landscape(clips):
    out = media.scale_video(clips["prores"], 480)
    assert streams(out[0]).display_size == (854, 480)


@pytest.mark.parametrize("name", [n for n in media.AUDIO_TOOLS if n != "Trim"])
def test_audio_tools(clips, name):
    out = media.tool(clips["song"], name)[0]
    i = streams(out)
    assert out.suffix == ".flac" and i.acodec == "flac"
    if name == "Trim silence":
        assert i.duration < 3.6
    if name == "Mono":
        assert "mono" in i.achannels
    if name == "Normalize":
        r = subprocess.run([FF, "-hide_banner", "-i", str(out), "-af", "ebur128", "-f", "null", "-"],
                           capture_output=True, text=True)
        lufs = [l for l in r.stderr.splitlines() if l.strip().startswith("I:")][-1]
        assert abs(float(lufs.split()[1]) + 14) < 1.0


def test_progress_and_cancel(clips, tmp_path):
    long = tmp_path / "long.mov"
    ff("-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=30", "-c:v", "mpeg4", "-q:v", "3", str(long))
    seen = []
    ev = threading.Event()

    def progress(frac, text):
        seen.append(text)
        if frac > 0.05:
            ev.set()

    with media.job_context(progress, ev):
        with pytest.raises(media.Cancelled):
            media.convert(long, "video", "HEVC")
    assert seen and seen[-1].endswith("%") or "left" in seen[-1]
    assert not list(tmp_path.glob("*HEVC*"))          # nothing half-written left behind
    assert not list(tmp_path.glob(".~*"))


def test_batch_progress_labels(clips):
    seen = []
    with media.job_context(lambda f, t: seen.append(t)):
        media.run_batch([clips["song"], clips["song"]], lambda p: media.convert(p, "audio", "OGG"))
    assert any(s.startswith("2/2") for s in seen)


def test_undecodable_raw_video_message(tmp_path):
    f = tmp_path / "A001.braw"
    f.write_bytes(b"\0" * 1024)
    with pytest.raises(Exception, match="DaVinci Resolve"):
        media.convert(f, "video", "MP4")


def test_report_summaries(clips):
    out = media.convert(clips["prores"], "video", "MP4")
    r = report.summarize("convert:MP4", [clips["prores"]], out)
    assert r.title == "MOV → MP4" and "1280×720" in r.detail and "0:02" in r.detail
    assert r.good.endswith("smaller")
    assert r.thumb is not None and r.thumb.exists()
    a = media.convert(clips["song"], "audio", "MP3")
    r = report.summarize("convert:MP3", [clips["song"]], a)
    assert r.title == "FLAC → MP3" and "0:04" in r.detail


# ---------------------------------------------------------------- photos
def test_heic_to_jpg_keeps_exif(tmp_path):
    if not convert.HEIF_OK:
        pytest.skip("no HEIF support")
    im = Image.new("RGB", (400, 300), (30, 140, 200))
    ex = Image.Exif()
    ex[0x010F] = "Apple"
    ex[0x0110] = "iPhone 15 Pro"
    src = tmp_path / "IMG_0001.HEIC"
    im.save(src, "HEIF", exif=ex.tobytes())
    out = convert.convert(src, "image", "JPG")[0]
    got = Image.open(out).getexif()
    assert out.suffix == ".jpg" and got.get(0x0110) == "iPhone 15 Pro"


import os
RAW_SAMPLE = Path(os.environ.get("DRAGSHIFT_RAW_SAMPLES", Path(__file__).with_name("samples")))


@pytest.mark.skipif(not convert.RAW_OK, reason="rawpy not installed")
def test_raw_photo_plugin_registered():
    from dragshift import rawimage
    assert Image.ID[0] == rawimage.FORMAT
    # a normal TIFF still opens as TIFF
    p = Path(__file__).parent / "_t.tif"
    Image.new("RGB", (10, 10)).save(p)
    try:
        assert Image.open(p).format == "TIFF"
    finally:
        p.unlink()


@pytest.mark.skipif(not convert.RAW_OK or not (RAW_SAMPLE.is_dir() and any(RAW_SAMPLE.glob("*"))), reason="no RAW samples")
def test_raw_photos_convert(tmp_path):
    import shutil
    for f in RAW_SAMPLE.iterdir():
        src = tmp_path / f.name
        shutil.copy(f, src)
        with Image.open(src) as im:
            assert im.format == "CAMERA_RAW" and min(im.size) > 1000
        out = convert.convert(src, "image", "JPG")[0]
        assert Image.open(out).size == Image.open(src).size
        assert Image.open(out).getexif().get(0x010F)  # camera make survived
