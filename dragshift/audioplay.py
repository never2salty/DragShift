"""Sound for the video player: FFmpeg decodes the audio track to PCM, sounddevice plays it.

The audio clock (frames actually handed to the sound card) is what the video follows, so
picture and sound stay in sync. If there is no sound device, playback simply stays silent.
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque

from . import media

RATE = 48000
CHANNELS = 2

try:
    import sounddevice as sd  # type: ignore
    SOUND_OK = True
except Exception:  # pragma: no cover  (no PortAudio)
    sd = None
    SOUND_OK = False


def atempo_chain(speed: float) -> str:
    """atempo only goes up to 2.0 per filter: 4× = atempo=2,atempo=2."""
    parts = []
    s = speed
    while s > 2.0:
        parts.append("atempo=2.0")
        s /= 2.0
    parts.append(f"atempo={s:.4f}")
    return ",".join(parts)


class AudioPlayer:
    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.stream = None
        self.buf: deque[bytes] = deque()
        self.pending = b""
        self.lock = threading.Lock()
        self.played_frames = 0
        self.t0 = 0.0
        self.speed = 1.0
        self.volume = 1.0
        self.muted = False
        self._reader: threading.Thread | None = None
        self._eof = False
        self.active = False

    # ------------------------------------------------ clock
    def position(self) -> float | None:
        """Video time the listener is hearing right now, or None when not playing sound."""
        if not self.active:
            return None
        with self.lock:
            played = self.played_frames
        lat = 0.0
        try:
            lat = float(self.stream.latency) if self.stream is not None else 0.0
        except Exception:
            pass
        return self.t0 + max(0.0, played / RATE - lat) * self.speed

    # ------------------------------------------------ control
    def start(self, src, t: float, speed: float, info: media.Info) -> bool:
        self.stop()
        if not SOUND_OK or not info.has_audio or speed > 4 or self.muted:
            return False
        af = [f"aresample={RATE}"]
        if speed != 1:
            af.append(atempo_chain(speed))
        args = [media.ffmpeg_exe(), "-hide_banner", "-nostdin", "-loglevel", "error", "-ss", f"{t:.4f}",
                "-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-ac", str(CHANNELS), "-af", ",".join(af),
                "-f", "s16le", "-acodec", "pcm_s16le", "-"]
        try:
            self.proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, creationflags=media.NO_WINDOW)
        except OSError:
            return False
        self.buf.clear()
        self.pending = b""
        self.played_frames = 0
        self._eof = False
        self.t0, self.speed = t, speed
        self._reader = threading.Thread(target=self._read, args=(self.proc,), daemon=True)
        self._reader.start()
        # a little audio in hand before the sound card starts asking
        end = time.monotonic() + 1.0
        while time.monotonic() < end and sum(len(b) for b in self.buf) < RATE * 4 // 10 and not self._eof:
            time.sleep(0.005)
        try:
            self.stream = sd.RawOutputStream(samplerate=RATE, channels=CHANNELS, dtype="int16",
                                             callback=self._callback, latency="low", blocksize=0)
            self.stream.start()
        except Exception:
            self.stream = None
            self._kill()
            return False
        self.active = True
        return True

    def stop(self):
        self.active = False
        st, self.stream = self.stream, None
        if st is not None:
            try:
                st.abort()
                st.close()
            except Exception:
                pass
        self._kill()

    def set_volume(self, v: float):
        self.volume = max(0.0, min(1.0, v))

    def _kill(self):
        p, self.proc = self.proc, None
        if p is not None and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    # ------------------------------------------------ threads
    def _read(self, proc: subprocess.Popen):
        try:
            while True:
                chunk = proc.stdout.read(8192)
                if not chunk:
                    break
                with self.lock:
                    self.buf.append(chunk)
                # don't run more than ~4 s ahead of the speakers
                while proc is self.proc and sum(len(b) for b in self.buf) > RATE * CHANNELS * 2 * 4:
                    time.sleep(0.02)
        finally:
            self._eof = True

    def _callback(self, outdata, frames, time_info, status):
        need = frames * CHANNELS * 2
        with self.lock:
            data = self.pending
            while len(data) < need and self.buf:
                data += self.buf.popleft()
            out, self.pending = data[:need], data[need:]
            self.played_frames += len(out) // (CHANNELS * 2)
        if self.volume < 0.999 and out:
            import numpy as np
            a = (np.frombuffer(out, dtype=np.int16) * self.volume).astype(np.int16)
            out = a.tobytes()
        if len(out) < need:
            out += b"\x00" * (need - len(out))
        outdata[:] = out
