import pytest


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    """History, grades and presets go to a per-test folder on every OS (macOS ignores LOCALAPPDATA)."""
    monkeypatch.setenv("DRAGSHIFT_DATA", str(tmp_path / "dragshift-data"))
