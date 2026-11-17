"""Run a function in the thread pool and get the result back on the GUI thread."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_LIVE: set = set()


class _Sig(QObject):
    done = Signal(object)
    failed = Signal(str)


class _Run(QRunnable):
    def __init__(self, fn, sig):
        super().__init__()
        self.fn, self.sig = fn, sig

    def run(self):
        try:
            r = self.fn()
        except Exception as e:  # noqa: BLE001
            from .util import UserError
            if not isinstance(e, UserError):
                traceback.print_exc()
            self.sig.failed.emit(str(e) or e.__class__.__name__)
            return
        self.sig.done.emit(r)


def run_bg(fn, done=None, failed=None):
    """fn() runs in a worker thread; done(result) / failed(message) run on the GUI thread."""
    sig = _Sig()
    _LIVE.add(sig)

    def _done(r):
        _LIVE.discard(sig)
        if done:
            done(r)

    def _failed(m):
        _LIVE.discard(sig)
        if failed:
            failed(m)
    sig.done.connect(_done)
    sig.failed.connect(_failed)
    QThreadPool.globalInstance().start(_Run(fn, sig))
    return sig
