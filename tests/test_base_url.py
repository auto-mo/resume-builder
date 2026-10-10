"""base_url (custom / local model endpoint) is refused unless ALLOW_CUSTOM_BASE_URL is set."""
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app as appmod  # noqa: E402

EXAMPLE = "http://example.invalid/v1"
MSG = "Local models work when you run Resume Builder yourself; see the GitHub README."


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ALLOW_CUSTOM_BASE_URL", raising=False)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _post(client, path, **extra):
    body = {"jd_text": "Analyst role", "api_key": "x", "base_url": EXAMPLE}
    body.update(extra)
    return client.post(path, json=body)


@pytest.mark.parametrize("path", ["/evaluate", "/cover-letter"])
def test_rejected_when_flag_off(client, path):
    with mock.patch.object(appmod.openai, "OpenAI") as oai:
        r = _post(client, path)
    assert r.status_code == 400
    assert r.get_json()["error"] == MSG
    oai.assert_not_called()


def test_rejected_on_get_and_upload(client):
    with mock.patch.object(appmod.openai, "OpenAI") as oai:
        assert client.get("/api/resumes", query_string={"base_url": EXAMPLE}).status_code == 400
        r = client.post("/upload-resume", data={"nickname": "n", "base_url": EXAMPLE})
        assert r.status_code == 400
    oai.assert_not_called()


def test_rejected_even_with_owner_code(client, monkeypatch):
    monkeypatch.setattr(appmod, "OWNER_ACCESS_CODE", "secret-code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    with mock.patch.object(appmod.openai, "OpenAI") as oai:
        r = _post(client, "/evaluate", api_key="secret-code")
    assert r.status_code == 400
    oai.assert_not_called()


def test_accepted_when_flag_on(client, monkeypatch):
    monkeypatch.setenv("ALLOW_CUSTOM_BASE_URL", "1")
    creds = appmod.resolve_credentials("", EXAMPLE)
    assert creds["provider"] == "local" and creds["base_url"] == EXAMPLE
    fake = mock.MagicMock()
    fake.chat.completions.create.return_value.choices = [
        mock.MagicMock(message=mock.MagicMock(content="{}"))]
    with mock.patch.object(appmod.openai, "OpenAI", return_value=fake) as oai:
        r = _post(client, "/cover-letter", api_key="")
    assert r.get_json().get("error") != MSG
    oai.assert_called_once()
    assert oai.call_args.kwargs["base_url"] == EXAMPLE


def test_empty_base_url_unaffected_when_off(client):
    assert client.get("/api/resumes", query_string={"base_url": ""}).status_code == 200
    assert appmod.resolve_credentials("sk-ant-abc", "")["provider"] == "anthropic"
    assert appmod.resolve_credentials("sk-abc", "   ")["provider"] == "openai"
    assert appmod.resolve_credentials("", "") is None


def test_owner_path_unaffected(monkeypatch):
    monkeypatch.delenv("ALLOW_CUSTOM_BASE_URL", raising=False)
    monkeypatch.setattr(appmod, "OWNER_ACCESS_CODE", "secret-code")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    c = appmod.resolve_credentials("secret-code", "")
    assert c["is_owner"] and c["provider"] == "anthropic" and c["base_url"] is None


def test_index_hides_field_when_off(client, monkeypatch):
    html = client.get("/").get_data(as_text=True)
    assert 'id="base-url-input"' not in html
    assert MSG in html
    monkeypatch.setenv("ALLOW_CUSTOM_BASE_URL", "1")
    assert 'id="base-url-input"' in client.get("/").get_data(as_text=True)
