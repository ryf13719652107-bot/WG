"""ui_auth signing / password sanity."""
import pytest

from app.services import ui_auth
from app.config import settings


@pytest.fixture(autouse=True)
def _isolate_ui_auth(monkeypatch):
    monkeypatch.delenv("WEB_UI_PASSWORD", raising=False)
    ui_auth.set_db_password_overlay(None)
    yield
    ui_auth.set_db_password_overlay(None)


def test_issue_verify_roundtrip(monkeypatch):
    monkeypatch.setattr(settings, "encryption_key", "ek-test")
    monkeypatch.setattr(settings, "web_ui_password", "secret-ui-pass")
    t = ui_auth.issue_token()
    assert ui_auth.verify_token(t)


def test_wrong_sig_rejected(monkeypatch):
    monkeypatch.setattr(settings, "encryption_key", "ek-test")
    monkeypatch.setattr(settings, "web_ui_password", "p1")
    t = ui_auth.issue_token()
    monkeypatch.setattr(settings, "web_ui_password", "p2")
    assert not ui_auth.verify_token(t)


def test_verify_password(monkeypatch):
    monkeypatch.setattr(settings, "web_ui_password", "my-pass")
    assert ui_auth.verify_password("my-pass")
    assert not ui_auth.verify_password("wrong")


def test_environment_overrides_settings(monkeypatch):
    monkeypatch.setattr(settings, "web_ui_password", "from-settings")
    monkeypatch.setenv("WEB_UI_PASSWORD", "from-env")
    assert ui_auth.auth_enabled()
    assert ui_auth.verify_password("from-env")
    assert not ui_auth.verify_password("from-settings")


def test_database_used_when_env_empty(monkeypatch):
    monkeypatch.setattr(settings, "web_ui_password", None)
    ui_auth.set_db_password_overlay("only-db")
    assert ui_auth.auth_enabled()
    assert ui_auth.verify_password("only-db")
