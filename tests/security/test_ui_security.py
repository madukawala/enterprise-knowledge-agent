"""The chat UI: what it renders, what it sends, and what its image contains."""

import json
import re
import tomllib
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import respx
import streamlit as st
from streamlit.testing.v1 import AppTest

from core.config import Settings
from tests.helpers.agents import FakeSearcher, hit, result_with
from tests.helpers.chat import answering_model, chat_app
from tests.helpers.ui import API_URL, APP_FILE, Playback, record
from ui.client import ChatClient
from ui.config import UiSettings

ROOT = Path(__file__).parents[2]


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[respx.MockRouter]:
    monkeypatch.delenv("API_URL", raising=False)
    st.cache_data.clear()
    with respx.mock(base_url=API_URL, assert_all_called=False) as router:
        router.get("/healthz").respond(200, json={"status": "ok"})
        yield router


def test_html_in_an_answer_is_shown_as_text_and_never_run(
    api: respx.MockRouter, settings: Settings
) -> None:
    # A poisoned document could lead the model to write HTML or script tags.
    answer = '<img src=x onerror="alert(1)"> Raise the pool size [1].'
    app = chat_app(settings, answering_model(answer), FakeSearcher([result_with(hit("RB-001"))]))
    api.post("/chat").mock(return_value=record(app, "pool?"))

    page = AppTest.from_file(str(APP_FILE), default_timeout=30).run()
    page = page.chat_input[0].set_value("pool?").run()

    assert any("onerror" in m.value for m in page.markdown)
    assert not any(m.proto.allow_html for m in page.markdown)
    assert not page.get("html")


def test_the_ui_never_sends_a_role_or_access_level_in_the_body(settings: Settings) -> None:
    app = chat_app(settings, answering_model("A [1]."), FakeSearcher([result_with(hit("RB-1"))]))
    playback = Playback(record(app, "pool?", headers={"X-Dev-Role": "admin"}))
    client = ChatClient(UiSettings(), transport=playback)

    list(client.ask("pool?", role="admin"))

    (request,) = playback.requests
    assert set(json.loads(request.content)) == {"message"}
    # The role travels only in the development header, which a production
    # API refuses (tests/security/test_chat_security.py).
    assert request.headers["x-dev-role"] == "admin"


def test_streamlit_keeps_its_protections_and_sends_nothing_out() -> None:
    config = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text())
    assert config["server"]["enableXsrfProtection"] is True
    assert config["server"]["enableCORS"] is True
    assert config["browser"]["gatherUsageStats"] is False
    # Set, so Streamlit does not look up the public IP on start.
    assert config["browser"]["serverAddress"] == "localhost"
    assert config["client"]["showErrorDetails"] in {"type", "none"}
    assert config["client"]["showErrorLinks"] is False


def test_the_ui_image_contains_no_backend_code() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text()
    ui_stage = dockerfile.split("AS ui\n", 1)[1]
    copied = re.findall(r"^COPY (?!--from)(\S+)", ui_stage, flags=re.MULTILINE)
    assert copied == ["ui", ".streamlit/config.toml"]
    # Its libraries are only the "ui" dependency group, not the backend's.
    ui_builder = dockerfile.split("AS ui-builder\n", 1)[1].split("FROM ", 1)[0]
    assert "--only-group ui" in ui_builder
    assert "--no-install-project" in ui_builder


def test_secrets_files_stay_out_of_git_and_the_images() -> None:
    for ignore_file in (".gitignore", ".dockerignore"):
        ignored = (ROOT / ignore_file).read_text().splitlines()
        assert ".streamlit/secrets.toml" in ignored


def test_connection_errors_do_not_show_internal_details() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connect to 10.0.3.7:8000 failed: [Errno 61]", request=request)

    client = ChatClient(UiSettings(), transport=httpx.MockTransport(refuse))
    with pytest.raises(Exception, match="Cannot reach") as caught:
        list(client.ask("pool?", role="viewer"))
    assert "10.0.3.7" not in str(caught.value)
    assert "Errno" not in str(caught.value)
