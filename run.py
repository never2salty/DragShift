"""Entry point for PyInstaller and for `python run.py`."""
import multiprocessing
import sys

from dragshift.handoff import try_handoff

if __name__ == "__main__":
    multiprocessing.freeze_support()  # a frozen exe must never re-run itself as a worker
    if try_handoff(sys.argv):
        sys.exit(0)
    from dragshift.app import main
    sys.exit(main())
