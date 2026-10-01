import importlib, pathlib, subprocess, sys
import pytest

AGENT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def load_server(tmp_path, monkeypatch):
    """Import a fresh `server` bound to a throwaway project dir (tmp_path)."""
    def _load(git=True):
        if git:
            subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
        monkeypatch.setenv("AGENT_PROJECT", str(tmp_path))
        monkeypatch.syspath_prepend(str(AGENT))
        sys.modules.pop("server", None)
        return importlib.import_module("server")
    return _load


@pytest.fixture
def srv(load_server):
    return load_server()
