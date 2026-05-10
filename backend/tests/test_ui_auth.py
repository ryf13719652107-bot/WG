"""ui_auth signing / password sanity."""
from app.services import ui_auth
from app.config import settings


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
