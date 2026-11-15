"""In-app updates from the GitHub releases of this fork.

Installed copy: downloads DragShift-Setup.exe and runs it silently; it closes the running app,
updates in place (settings, history and the tray icon stay) and starts the new version.
Portable copy: downloads DragShift.exe next to the running one and swaps it with a tiny .bat.
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .util import UserError

REPO = "never2salty/DragShift"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0
DETACHED = 0x00000008 if IS_WIN else 0


@dataclass
class Release:
    version: str
    notes: str
    setup_url: str | None
    portable_url: str | None
    page: str
    mac_url: str | None = None          # the .dmg for this Mac's chip


def parse_version(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:4]) or (0,)


def newer(remote: str, local: str = __version__) -> bool:
    return parse_version(remote) > parse_version(local)


def _ctx():
    try:
        import certifi  # type: ignore
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _get(url: str, timeout: float = 15):
    req = urllib.request.Request(url, headers={"User-Agent": f"DragShift/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout, context=_ctx())


def latest() -> Release:
    try:
        with _get(API) as r:
            data = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UserError("No public release found. Updates work when the GitHub repository "
                            f"{REPO} is public.") from None
        raise UserError(f"GitHub answered {e.code}. Try again later.") from None
    except Exception as e:
        raise UserError(f"Couldn't reach GitHub ({e.__class__.__name__}). Check your internet connection.") from None
    assets = {a["name"].lower(): a["browser_download_url"] for a in data.get("assets", [])}
    return Release(version=data.get("tag_name", "").lstrip("v"), notes=data.get("body") or "",
                   setup_url=assets.get("dragshift-setup.exe"), portable_url=assets.get("dragshift.exe"),
                   page=data.get("html_url", f"https://github.com/{REPO}/releases"),
                   mac_url=assets.get(mac_asset_name()))


def mac_asset_name() -> str:
    import platform
    return "dragshift-mac-apple-silicon.dmg" if platform.machine() == "arm64" else "dragshift-mac-intel.dmg"


def mac_app_bundle() -> Path | None:
    """…/DragShift.app of the running frozen app on macOS."""
    if not (IS_MAC and getattr(sys, "frozen", False)):
        return None
    for p in Path(sys.executable).resolve().parents:
        if p.suffix == ".app":
            return p
    return None


def is_installed() -> bool:
    """True for the copy installed by DragShift-Setup.exe (%LOCALAPPDATA%\\Programs\\DragShift)."""
    if not getattr(sys, "frozen", False):
        return False
    exe = Path(sys.executable).resolve()
    return (exe.parent / "unins000.exe").exists()


def can_self_update() -> bool:
    if IS_MAC:
        return mac_app_bundle() is not None
    return IS_WIN and getattr(sys, "frozen", False)


def download(url: str, dest: Path, progress=None, cancelled=lambda: False) -> Path:
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _get(url, timeout=60) as r, open(tmp, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            if cancelled():
                f.close()
                tmp.unlink(missing_ok=True)
                from .media import Cancelled
                raise Cancelled()
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if progress and total:
                progress(got / total)
    if total and tmp.stat().st_size != total:
        tmp.unlink(missing_ok=True)
        raise UserError("The download was incomplete. Please try again.")
    os.replace(tmp, dest)
    return dest


def fetch_update(rel: Release) -> Path:
    """Download what this copy needs (installer or portable exe). Runs in a worker thread."""
    from . import media
    if not can_self_update():
        raise UserError("Automatic updates work in the installed app. Running from source: git pull instead.")

    def progress(frac):
        media._report(frac)

    if IS_MAC:
        if not rel.mac_url:
            raise UserError("This release has no Mac download for this chip yet.")
        dest = Path(tempfile.gettempdir()) / f"DragShift-{rel.version}.dmg"
        return download(rel.mac_url, dest, progress, media._cancelled)

    if is_installed():
        if not rel.setup_url:
            raise UserError("This release has no DragShift-Setup.exe.")
        dest = Path(tempfile.gettempdir()) / f"DragShift-Setup-{rel.version}.exe"
        return download(rel.setup_url, dest, progress, media._cancelled)
    if not rel.portable_url:
        raise UserError("This release has no portable DragShift.exe.")
    dest = Path(sys.executable).with_name(f"DragShift-{rel.version}.new.exe")
    return download(rel.portable_url, dest, progress, media._cancelled)


def mac_update_script(dmg: Path, app: Path, pid: int) -> str:
    """Shell script: wait for DragShift to quit, swap DragShift.app from the .dmg, open it again."""
    q = lambda p: "'" + str(p).replace("'", "'\\''") + "'"  # noqa: E731
    return f"""#!/bin/bash
# DragShift update: replace the app bundle once the running copy has quit
while kill -0 {pid} 2>/dev/null; do sleep 0.3; done
MNT=$(mktemp -d)
hdiutil attach {q(dmg)} -mountpoint "$MNT" -nobrowse -quiet || exit 1
rm -rf {q(app)}.old
mv {q(app)} {q(app)}.old && ditto "$MNT/DragShift.app" {q(app)} && rm -rf {q(app)}.old || mv {q(app)}.old {q(app)}
xattr -dr com.apple.quarantine {q(app)} 2>/dev/null
hdiutil detach "$MNT" -quiet
rm -f {q(dmg)}
open {q(app)}
rm -f "$0"
"""


def apply_update(downloaded: Path) -> None:
    """Start the swap/installer, then the caller quits the app."""
    if IS_MAC:
        app = mac_app_bundle()
        script = Path(tempfile.gettempdir()) / "dragshift-update.sh"
        script.write_text(mac_update_script(downloaded, app, os.getpid()), encoding="utf-8")
        script.chmod(0o755)
        subprocess.Popen(["/bin/bash", str(script)], start_new_session=True, close_fds=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if is_installed():
        subprocess.Popen([str(downloaded), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                          "/CLOSEAPPLICATIONS", "/LAUNCH"], creationflags=DETACHED, close_fds=True)
        return
    exe = Path(sys.executable).resolve()
    bat = Path(tempfile.gettempdir()) / "dragshift-update.bat"
    bat.write_text(
        "@echo off\r\n"
        "rem DragShift portable update: wait for the old exe to close, swap, restart\r\n"
        f"set OLD=\"{exe}\"\r\nset NEW=\"{downloaded}\"\r\n"
        ":wait\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        "move /y %NEW% %OLD% >nul 2>&1\r\n"
        "if errorlevel 1 goto wait\r\n"
        "start \"\" %OLD% --quiet\r\n"
        "del \"%~f0\"\r\n", encoding="utf-8")
    subprocess.Popen(["cmd", "/c", str(bat)], creationflags=NO_WINDOW | DETACHED, close_fds=True)
