"""Check programs and archives for malware without opening a browser.

* Windows Defender scans the file locally (MpCmdRun, no internet, no key).
* The digital signature says who published it (Authenticode / codesign).
* VirusTotal (free API key in Settings) looks the SHA-256 up in 70+ engines. Only the hash leaves
  the computer, unless you press "Upload" for a file VirusTotal has never seen.
"""
from __future__ import annotations

import functools
import glob
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .util import UserError, human_size

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = 0x08000000 if IS_WIN else 0
VT = "https://www.virustotal.com/api/v3"
MAX_UPLOAD = 650 * 1024 * 1024


@dataclass
class Check:
    state: str = "pending"      # pending | clean | threat | suspicious | unknown | unsigned | signed | off | error
    text: str = ""
    details: list[str] = field(default_factory=list)


@dataclass
class Report:
    path: Path
    size: int = 0
    sha256: str = ""
    signature: Check = field(default_factory=Check)
    defender: Check = field(default_factory=Check)
    vt: Check = field(default_factory=Check)
    vt_link: str = ""

    def verdict(self) -> tuple[str, str]:
        """(level, sentence) – level is bad | warn | good | unknown."""
        if "threat" in (self.defender.state, self.vt.state):
            return "bad", "Malware found – don't run this file."
        if self.vt.state == "suspicious":
            return "warn", "A few engines flag this file. Be careful."
        if self.signature.state == "error":
            return "warn", "The signature is broken – the file was changed after signing."
        if self.defender.state == "clean" and self.vt.state == "clean":
            return "good", "No threats found."
        if self.vt.state == "clean":
            return "good", "No engine on VirusTotal flags this file."
        if self.defender.state == "clean":
            return "good", "Windows Defender found nothing."
        return "unknown", "Not enough information yet."


def sha256(path: str | Path, progress=None) -> str:
    h = hashlib.sha256()
    size = max(1, os.path.getsize(path))
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done / size)
    return h.hexdigest()


# ---------------------------------------------------------------- Windows Defender
def defender_exe() -> Path | None:
    if not IS_WIN:
        return None
    pd = os.environ.get("ProgramData", r"C:\ProgramData")
    plat = sorted(glob.glob(os.path.join(pd, "Microsoft", "Windows Defender", "Platform", "*", "MpCmdRun.exe")),
                  key=lambda p: [int(x) if x.isdigit() else 0 for x in re.split(r"[.-]", Path(p).parent.name)])
    if plat:
        return Path(plat[-1])
    p = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Windows Defender" / "MpCmdRun.exe"
    return p if p.exists() else None


def parse_defender(code: int, out: str) -> Check:
    threats = [m.strip() for m in re.findall(r"Threat\s*:\s*(.+)", out)]
    if code == 0 and not threats:
        return Check("clean", "Windows Defender: no threats")
    if code == 2 or threats:
        return Check("threat", f"Windows Defender: {threats[0] if threats else 'threat found'}", threats)
    low = out.lower()
    if "0x800106ba" in low or "service" in low and "not" in low:
        return Check("off", "Windows Defender is turned off (another antivirus is active)")
    return Check("error", f"Windows Defender couldn't scan (code {code})")


def defender_scan(path: Path) -> Check:
    exe = defender_exe()
    if exe is None:
        return Check("off", "Local scan: only on Windows" if not IS_WIN else "Windows Defender not found")
    try:
        p = subprocess.run([str(exe), "-Scan", "-ScanType", "3", "-File", str(path), "-DisableRemediation"],
                           capture_output=True, timeout=600, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return Check("error", "Windows Defender took too long")
    except OSError as e:
        return Check("error", f"Windows Defender couldn't start ({e.__class__.__name__})")
    out = (p.stdout + p.stderr).decode("utf-8", "replace")
    return parse_defender(p.returncode, out)


# ---------------------------------------------------------------- signatures
def _cn(subject: str) -> str:
    m = re.search(r"CN=(\"[^\"]+\"|[^,]+)", subject or "")
    return (m.group(1).strip('"') if m else subject or "").strip()


def signature(path: Path) -> Check:
    try:
        if IS_WIN:
            lit = str(path).replace("'", "''")
            cmd = (f"$s = Get-AuthenticodeSignature -LiteralPath '{lit}'; "
                   "[pscustomobject]@{Status=[string]$s.Status; Signer=[string]$s.SignerCertificate.Subject} "
                   "| ConvertTo-Json -Compress")
            p = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                               capture_output=True, timeout=60, creationflags=NO_WINDOW)
            d = json.loads(p.stdout.decode("utf-8", "replace") or "{}")
            st, who = d.get("Status", ""), _cn(d.get("Signer", ""))
            if st == "Valid":
                return Check("signed", f"Signed by {who}")
            if st == "NotSigned":
                return Check("unsigned", "Not digitally signed")
            if st == "HashMismatch":
                return Check("error", f"Signature broken – changed after signing ({who})")
            if st:
                return Check("unsigned", f"Signature not trusted ({st}{', ' + who if who else ''})")
            return Check("unsigned", "No signature information")
        if IS_MAC:
            if path.suffix.lower() == ".pkg":
                p = subprocess.run(["pkgutil", "--check-signature", str(path)], capture_output=True, timeout=60)
                out = p.stdout.decode("utf-8", "replace")
                m = re.search(r"1\. (.+)", out)
                return Check("signed", f"Signed by {m.group(1).strip()}") if p.returncode == 0 and m else \
                    Check("unsigned", "Not signed")
            p = subprocess.run(["codesign", "-dv", "--verbose=2", str(path)], capture_output=True, timeout=60)
            out = p.stderr.decode("utf-8", "replace")
            m = re.search(r"Authority=(.+)", out)
            if p.returncode == 0 and m:
                return Check("signed", f"Signed by {m.group(1).strip()}")
            return Check("unsigned", "Not signed")
    except Exception as e:  # noqa: BLE001
        return Check("unsigned", f"Signature not checked ({e.__class__.__name__})")
    return Check("unsigned", "Signature check not available here")


# ---------------------------------------------------------------- VirusTotal
def _ctx():
    from .updater import _ctx as c
    return c()


def _vt(method: str, url: str, key: str, body: bytes | None = None, ctype: str | None = None,
        timeout: float = 60) -> dict:
    headers = {"x-apikey": key, "accept": "application/json", "User-Agent": "DragShift"}
    if ctype:
        headers["content-type"] = ctype
    req = urllib.request.Request(url if url.startswith("http") else VT + url, data=body, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_ctx()) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FileNotFoundError from None
        if e.code in (401, 403):
            raise UserError("VirusTotal didn't accept the API key. Check it in Settings → Security.") from None
        if e.code == 429:
            raise UserError("VirusTotal limit reached (free key: 4 lookups per minute, 500 per day). "
                            "Try again in a minute.") from None
        raise UserError(f"VirusTotal answered {e.code}.") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise UserError(f"Couldn't reach VirusTotal ({e.__class__.__name__}).") from None


def parse_vt(data: dict) -> Check:
    a = (data.get("data") or {}).get("attributes") or {}
    stats = a.get("last_analysis_stats") or {}
    mal, sus = int(stats.get("malicious", 0)), int(stats.get("suspicious", 0))
    total = sum(int(stats.get(k, 0)) for k in ("malicious", "suspicious", "undetected", "harmless"))
    res = a.get("last_analysis_results") or {}
    hits = sorted(f"{eng}: {r.get('result') or r.get('category')}" for eng, r in res.items()
                  if r.get("category") in ("malicious", "suspicious"))
    label = ((a.get("popular_threat_classification") or {}).get("suggested_threat_label") or "")
    when = a.get("last_analysis_date")
    age = ""
    if when:
        days = int((time.time() - int(when)) // 86400)
        age = " · checked today" if days <= 0 else f" · checked {days} day{'s' if days != 1 else ''} ago"
    if not total:
        return Check("pending", "VirusTotal is still analysing…")
    if mal >= 3 or (mal and label):
        return Check("threat", f"VirusTotal: {mal} of {total} engines flag it" + (f" ({label})" if label else "") + age,
                     hits)
    if mal or sus:
        return Check("suspicious", f"VirusTotal: {mal + sus} of {total} engines flag it" + age, hits)
    return Check("clean", f"VirusTotal: 0 of {total} engines flag it" + age)


def vt_lookup(sha: str, key: str) -> Check:
    if not key:
        return Check("off", "VirusTotal: add your free API key in Settings → Security")
    try:
        return parse_vt(_vt("GET", f"/files/{sha}", key))
    except FileNotFoundError:
        return Check("unknown", "VirusTotal has never seen this file")


def _multipart(path: Path) -> tuple[bytes, str]:
    b = uuid.uuid4().hex
    head = (f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    return head + path.read_bytes() + f"\r\n--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def vt_upload(path: Path, sha: str, key: str, progress=None) -> Check:
    """Upload, then wait for the analysis (usually 30–90 s)."""
    if not key:
        raise UserError("Add your VirusTotal API key in Settings → Security first.")
    size = path.stat().st_size
    if size > MAX_UPLOAD:
        raise UserError(f"VirusTotal takes files up to 650 MB (this one is {human_size(size)}).")
    url = "/files"
    if size > 32 * 1024 * 1024:
        url = _vt("GET", "/files/upload_url", key).get("data")
    body, ctype = _multipart(path)
    an = _vt("POST", url, key, body, ctype, timeout=900)
    aid = (an.get("data") or {}).get("id")
    if not aid:
        raise UserError("VirusTotal didn't accept the upload.")
    t0 = time.time()
    while time.time() - t0 < 600:
        if progress:
            progress(min(0.95, (time.time() - t0) / 90))
        time.sleep(15)
        st = _vt("GET", f"/analyses/{aid}", key)
        if ((st.get("data") or {}).get("attributes") or {}).get("status") == "completed":
            break
    return vt_lookup(sha, key)


def scan(path: str | Path, key: str = "", local: bool = True, step=None) -> Report:
    """Everything except uploading. step(report) is called after each part (for live updates)."""
    path = Path(path)
    if path.is_dir():
        raise UserError("Drop a file, not a folder.")
    r = Report(path, path.stat().st_size)
    r.sha256 = sha256(path)
    r.vt_link = f"https://www.virustotal.com/gui/file/{r.sha256}"
    step and step(r)
    r.signature = signature(path)
    step and step(r)
    try:
        r.vt = vt_lookup(r.sha256, key)
    except UserError as e:
        r.vt = Check("error", str(e))
    step and step(r)
    r.defender = defender_scan(path) if local else Check("off", "Local scan skipped")
    step and step(r)
    return r


# ---------------------------------------------------------------- window
@functools.cache
def _window_classes():
    from PySide6.QtCore import QObject, Qt, Signal
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import (QFileDialog, QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea,
                                   QVBoxLayout, QWidget)

    from .bg import run_bg
    from .window import GlassWindow

    COLORS = {"bad": "#ff6b6b", "warn": "#ffd36b", "good": "#8dffb8", "unknown": "#c9c3d6"}
    DOT = {"clean": "#8dffb8", "signed": "#8dffb8", "threat": "#ff6b6b", "error": "#ff6b6b",
           "suspicious": "#ffd36b", "unsigned": "#ffd36b", "unknown": "#c9c3d6", "off": "#8a8496",
           "pending": "#8a8496"}

    class _Live(QObject):
        update = Signal(object)

    class FileCard(QFrame):
        def __init__(self, path: Path, owner):
            super().__init__()
            self.setObjectName("card")
            self.owner, self.path, self.report = owner, path, None
            v = QVBoxLayout(self)
            v.setContentsMargins(14, 12, 14, 12)
            v.setSpacing(6)
            top = QHBoxLayout()
            self.name = QLabel(path.name)
            self.name.setStyleSheet("font-weight: 700; font-size: 11pt;")
            top.addWidget(self.name, 1)
            self.size = QLabel("")
            self.size.setObjectName("dim")
            top.addWidget(self.size)
            rm = QPushButton("✕")
            rm.setObjectName("chrome")
            rm.setToolTip("Remove from the list")
            rm.clicked.connect(lambda: owner.remove(self))
            top.addWidget(rm)
            v.addLayout(top)
            self.verdict = QLabel("Scanning…")
            self.verdict.setStyleSheet("font-weight: 700; font-size: 12pt;")
            v.addWidget(self.verdict)
            self.rows = {}
            for k in ("signature", "defender", "vt"):
                lab = QLabel("…")
                lab.setWordWrap(True)
                lab.setTextFormat(Qt.RichText)
                v.addWidget(lab)
                self.rows[k] = lab
            self.hits = QLabel("")
            self.hits.setObjectName("dim")
            self.hits.setWordWrap(True)
            self.hits.hide()
            v.addWidget(self.hits)
            hrow = QHBoxLayout()
            self.hash = QLabel("SHA-256 …")
            self.hash.setObjectName("dim")
            self.hash.setTextInteractionFlags(Qt.TextSelectableByMouse)
            hrow.addWidget(self.hash, 1)
            self.copy = QPushButton("Copy hash")
            self.copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.report.sha256 if self.report else ""))
            hrow.addWidget(self.copy)
            self.upload = QPushButton("Upload to VirusTotal")
            self.upload.setToolTip("Only for files VirusTotal doesn't know yet. Uploaded files can be downloaded by\n"
                                   "VirusTotal's paying customers – don't upload anything private.")
            self.upload.clicked.connect(self.do_upload)
            self.upload.hide()
            hrow.addWidget(self.upload)
            self.again = QPushButton("Scan again")
            self.again.clicked.connect(self.start)
            hrow.addWidget(self.again)
            v.addLayout(hrow)
            self.live = _Live()
            self.live.update.connect(self.show_report)

        def start(self):
            self.verdict.setText("Scanning…")
            self.verdict.setStyleSheet("font-weight: 700; font-size: 12pt;")
            self.upload.hide()
            self.again.setEnabled(False)
            for lab in self.rows.values():
                lab.setText(f"<span style='color:{DOT['pending']}'>●</span> …")
            key = self.owner.vt_key()
            run_bg(lambda: scan(self.path, key, step=self.live.update.emit), self.finished, self.failed)

        def _row(self, c: Check) -> str:
            import html
            return f"<span style='color:{DOT.get(c.state, '#ccc')}'>●</span>&nbsp; {html.escape(c.text or '…')}"

        def show_report(self, r: Report):
            self.report = r
            self.size.setText(human_size(r.size))
            self.hash.setText(f"SHA-256 {r.sha256[:16]}…{r.sha256[-8:]}" if r.sha256 else "SHA-256 …")
            self.rows["signature"].setText(self._row(r.signature))
            self.rows["defender"].setText(self._row(r.defender))
            self.rows["vt"].setText(self._row(r.vt))
            if r.vt.details or r.defender.details:
                det = r.defender.details + r.vt.details
                self.hits.setText(" · ".join(det[:12]) + (f" · +{len(det) - 12} more" if len(det) > 12 else ""))
                self.hits.show()
            else:
                self.hits.hide()

        def finished(self, r: Report):
            self.show_report(r)
            level, text = r.verdict()
            self.verdict.setText(text)
            self.verdict.setStyleSheet(f"font-weight: 700; font-size: 12pt; color: {COLORS[level]};")
            self.upload.setVisible(r.vt.state == "unknown")
            self.again.setEnabled(True)

        def failed(self, msg):
            self.verdict.setText(msg)
            self.verdict.setStyleSheet(f"font-weight: 700; color: {COLORS['bad']};")
            self.again.setEnabled(True)

        def do_upload(self):
            r = self.report
            if r is None:
                return
            self.upload.setEnabled(False)
            self.upload.setText("Uploading… (about a minute)")
            key = self.owner.vt_key()

            def done(c):
                r.vt = c
                self.upload.setText("Upload to VirusTotal")
                self.upload.setEnabled(True)
                self.finished(r)

            def failed(m):
                self.upload.setText("Upload to VirusTotal")
                self.upload.setEnabled(True)
                self.rows["vt"].setText(self._row(Check("error", m)))
            run_bg(lambda: vt_upload(r.path, r.sha256, key), done, failed)

    class ScanWindow(GlassWindow):
        def __init__(self, paths=(), settings=None):
            super().__init__("Virus scan", size=(640, 620))
            self.settings = settings
            self.setAcceptDrops(True)
            self.hint = QLabel("Drop programs, installers or archives here (.exe, .msi, .zip, .dmg …).\n"
                               "Windows Defender scans locally; VirusTotal checks the fingerprint in 70+ engines.")
            self.hint.setObjectName("dim")
            self.hint.setWordWrap(True)
            self.body.addWidget(self.hint)
            if not self.vt_key():
                self.keyhint = QLabel("Tip: add a free VirusTotal API key in Settings → Security for the 70-engine check.")
                self.keyhint.setObjectName("warn")
                self.keyhint.setWordWrap(True)
                self.body.addWidget(self.keyhint)
            self.list = QVBoxLayout()
            self.list.setSpacing(10)
            self.list.addStretch(1)
            holder = QWidget()
            holder.setLayout(self.list)
            holder.setStyleSheet("background: transparent;")
            sc = QScrollArea()
            sc.setWidgetResizable(True)
            sc.setFrameShape(QFrame.NoFrame)
            sc.setStyleSheet("QScrollArea { background: transparent; }")
            sc.setWidget(holder)
            self.body.addWidget(sc, 1)
            row = QHBoxLayout()
            pick = QPushButton("Choose files…")
            pick.clicked.connect(self.pick)
            row.addWidget(pick)
            row.addStretch(1)
            done = QPushButton("Done")
            done.setObjectName("primary")
            done.clicked.connect(self.close)
            row.addWidget(done)
            self.body.addLayout(row)
            self.cards: list[FileCard] = []
            self.add_paths(list(paths))

        def vt_key(self) -> str:
            try:
                return (self.settings.get("vt_key") or "").strip() if self.settings else ""
            except Exception:
                return ""

        def add_paths(self, paths):
            for p in paths:
                p = Path(p)
                if p.is_dir():
                    files = [f for f in sorted(p.iterdir()) if f.is_file()][:20]
                else:
                    files = [p]
                for f in files:
                    if any(c.path == f for c in self.cards):
                        continue
                    c = FileCard(f, self)
                    self.cards.append(c)
                    self.list.insertWidget(self.list.count() - 1, c)
                    c.start()

        def remove(self, card):
            if card in self.cards:
                self.cards.remove(card)
            card.deleteLater()

        def pick(self):
            files, _ = QFileDialog.getOpenFileNames(self, "Files to scan")
            self.add_paths(files)

        def dragEnterEvent(self, e):
            if e.mimeData().hasUrls():
                e.acceptProposedAction()

        def dropEvent(self, e):
            self.add_paths([u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()])

    return ScanWindow


def ScanWindow(paths=(), settings=None):
    return _window_classes()(paths, settings)
