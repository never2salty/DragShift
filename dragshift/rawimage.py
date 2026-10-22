"""Camera RAW photos (CR2, CR3, NEF, ARW, DNG, RAF, ORF, RW2…) for Pillow.

Registers a read-only Pillow plugin backed by LibRaw (rawpy), so `Image.open("IMG_0001.CR3")`
works everywhere in DragShift: conversions, every tool window, previews and notifications.
The photo is developed with the camera's white balance, like the camera's own JPEG.
"""
from __future__ import annotations

import os

from PIL import Image, ImageFile

from .util import RAW_EXTS

try:
    import rawpy  # type: ignore
    RAW_OK = True
except Exception:  # pragma: no cover
    rawpy = None
    RAW_OK = False

FORMAT = "CAMERA_RAW"


def _is_raw_name(name) -> bool:
    return isinstance(name, (str, os.PathLike)) and os.path.splitext(str(name))[1].lower() in RAW_EXTS


def _accept(prefix: bytes) -> bool:
    # RAW files mostly look like TIFF (or ISO-BMFF for CR3); the extension decides in _open
    return True


class CameraRawImageFile(ImageFile.ImageFile):
    format = FORMAT
    format_description = "Camera RAW (LibRaw)"

    def _open(self):
        name = getattr(self, "filename", None) or getattr(self.fp, "name", None)
        if not RAW_OK or not _is_raw_name(name):
            raise SyntaxError("not a camera RAW file")
        self._raw_path = str(name)
        try:
            with rawpy.imread(self._raw_path) as raw:
                s = raw.sizes
                w, h = s.width, s.height
                if s.flip in (5, 6):  # developed image is rotated 90°
                    w, h = h, w
        except Exception as e:
            raise SyntaxError(f"LibRaw can't read this file: {e}") from e
        self._mode = "RGB"
        self._size = (w, h)
        self.tile = []
        self._developed = None
        self._half = False
        self.info["exif"] = _exif_bytes(self._raw_path)

    def draft(self, mode, size):
        """Fast half-size decode when only a preview is needed."""
        if size and self._developed is None and size[0] * 2 <= self.size[0] and size[1] * 2 <= self.size[1]:
            self._half = True
            self._size = (self.size[0] // 2, self.size[1] // 2)
            return mode, (0, 0, *self.size)
        return None

    def load(self):
        if self._developed is None:
            with rawpy.imread(self._raw_path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, output_bps=8, half_size=self._half,
                                      no_auto_bright=False, bright=1.0)
            im = Image.fromarray(rgb, "RGB")
            if im.size != self.size:
                self._size = im.size
            self._developed = im
            self.im = im.im
        return Image.Image.load(self)


def _exif_bytes(path: str) -> bytes:
    """Camera, lens, date, GPS… from the RAW's TIFF header (orientation reset: LibRaw already rotated)."""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        if head[:2] not in (b"II", b"MM"):
            return b""
        from PIL import TiffImagePlugin
        with TiffImagePlugin.TiffImageFile(path) as t:
            ex = t.getexif()
            if 0x0112 in ex:
                ex[0x0112] = 1
            # keep only descriptive tags; drop strip/tile layout of the RAW data itself
            for tag in (0x0100, 0x0101, 0x0102, 0x0103, 0x0106, 0x0111, 0x0115, 0x0116, 0x0117,
                        0x011C, 0x0142, 0x0143, 0x0144, 0x0145, 0x014A, 0x0201, 0x0202, 0x02BC):
                ex.pop(tag, None)
            # the maker note can be > 64 KB, more than a JPG header holds
            exif_ifd = ex.get_ifd(0x8769)
            exif_ifd.pop(0x927C, None)
            data = ex.tobytes()
            return data if len(data) < 60000 else b""
    except Exception:
        return b""


if RAW_OK:
    Image.register_open(FORMAT, CameraRawImageFile, _accept)
    Image.register_extensions(FORMAT, sorted(RAW_EXTS))
    # run before the TIFF plugin, which would otherwise grab TIFF-based RAWs and show the tiny thumbnail
    if FORMAT in Image.ID:
        Image.ID.remove(FORMAT)
    Image.ID.insert(0, FORMAT)
