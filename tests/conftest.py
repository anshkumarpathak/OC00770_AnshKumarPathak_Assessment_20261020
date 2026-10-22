import pytest

from mock_apis import launcher


@pytest.fixture(scope="session", autouse=True)
def mock_apis():
    launcher.ensure_running()
    yield


@pytest.fixture(autouse=True)
def clean_state(tmp_path, monkeypatch):
    launcher.reset_all()
    monkeypatch.setattr("support_agent.config.RUNTIME_DIR", tmp_path)
    yield
    launcher.reset_all()
