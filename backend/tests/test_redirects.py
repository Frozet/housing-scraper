import json
import threading
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app import collector
from app.collector import Blocked, CollectionError, Fetcher, collect
from app.config import Settings
from app.models import CollectionRun
from app.parser import CATALOG_URL, ParseError, client_redirect, parse_catalog
from app.repository import create_run

TARGET = "https://www.avito.ru/irkutsk/kvartiry/prodam-ASgBAgICAUSSA8YQ?localPriority=0"


def redirect_html(target, **updates):
    data = {"status": {"code": 301}, "redirected": True, "url": target, **updates}
    return '<script type="mime/invalid">' + json.dumps({"loaderData": {"data": data}}) + '</script>'


@pytest.fixture
def fetcher(monkeypatch):
    fetcher = Fetcher(Settings(request_delay_seconds=1), threading.Event())
    monkeypatch.setattr(fetcher, "pause", lambda seconds: None)
    yield fetcher
    fetcher.close()


def test_observed_http_200_router_response_is_followed(fetcher, monkeypatch, url):
    html = (Path(__file__).parent / "fixtures/catalog-redirect.html").read_text()
    assert client_redirect(html) == TARGET.removeprefix("https://www.avito.ru")
    catalog = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    calls = []
    def get(target):
        calls.append(target)
        return httpx.Response(200, text=html if len(calls) == 1 else catalog)
    monkeypatch.setattr(fetcher.client, "get", get)
    assert parse_catalog(fetcher.get(CATALOG_URL + "?p=1")) == ([url], False)
    assert calls == [CATALOG_URL + "?p=1", TARGET]
    assert fetcher.last_url == TARGET


@pytest.mark.parametrize("target", [
    "https://evil.example/irkutsk/kvartiry/prodam", "//evil.example/catalog",
    "https://www.avito.ru/angarsk/kvartiry/prodam", "https://www.avito.ru/irkutsk/doma/prodam",
    "http://www.avito.ru/irkutsk/kvartiry/prodam", "https://www.avito.ru:8443/irkutsk/kvartiry/prodam",
    "https://login@www.avito.ru/irkutsk/kvartiry/prodam",
])
def test_redirect_cannot_leave_allowed_section(fetcher, monkeypatch, target):
    calls = []
    def get(url):
        calls.append(url)
        return httpx.Response(200, text=redirect_html(target))
    monkeypatch.setattr(fetcher.client, "get", get)
    with pytest.raises(CollectionError, match="за пределы"):
        fetcher.get(CATALOG_URL)
    assert calls == [CATALOG_URL]


def test_redirect_loop_is_not_retried(fetcher, monkeypatch):
    calls = []
    def get(url):
        calls.append(url)
        return httpx.Response(200, text=redirect_html(CATALOG_URL))
    monkeypatch.setattr(fetcher.client, "get", get)
    with pytest.raises(CollectionError, match="циклическое"):
        fetcher.get(CATALOG_URL)
    assert len(calls) == 1


def test_access_block_after_redirect_is_terminal(fetcher, monkeypatch):
    calls = []
    def get(url):
        calls.append(url)
        return httpx.Response(200, text=redirect_html(TARGET)) if len(calls) == 1 else httpx.Response(429)
    monkeypatch.setattr(fetcher.client, "get", get)
    with pytest.raises(Blocked, match="429"):
        fetcher.get(CATALOG_URL)
    assert len(calls) == 2


def test_redirect_keeps_session_cookies(fetcher):
    calls = []
    def respond(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(200, text=redirect_html(TARGET),
                                  headers={"Set-Cookie": "catalog_session=test; Path=/; Secure"})
        assert request.headers.get("Cookie") == "catalog_session=test"
        return httpx.Response(200, text="<h1>Каталог</h1>")
    fetcher.client.close()
    fetcher.client = httpx.Client(transport=httpx.MockTransport(respond))
    assert fetcher.get(CATALOG_URL) == "<h1>Каталог</h1>"
    assert len(calls) == 2


def test_regular_http_redirect_and_router_redirect_can_be_combined(fetcher, monkeypatch):
    calls = []
    def get(url):
        calls.append(url)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": TARGET})
        if len(calls) == 2:
            return httpx.Response(200, text=redirect_html(TARGET + "&p=2"))
        return httpx.Response(200, text="<h1>Каталог</h1>")
    monkeypatch.setattr(fetcher.client, "get", get)
    assert fetcher.get(CATALOG_URL) == "<h1>Каталог</h1>"
    assert calls == [CATALOG_URL, TARGET, TARGET + "&p=2"]


def test_redirect_payload_requires_explicit_flag_status_and_destination():
    assert client_redirect(redirect_html(TARGET, redirected=False)) is None
    assert client_redirect(redirect_html(TARGET, status={"code": 200})) is None
    assert client_redirect('<script type="mime/invalid">not JSON</script>') is None
    assert client_redirect('<script>window.location="https://evil.example";</script>') is None
    with pytest.raises(ParseError, match="без адреса"):
        client_redirect(redirect_html(None))


def test_pagination_preserves_canonical_catalog_and_filters(session_factory, monkeypatch, html, url):
    monkeypatch.setattr(collector, "SessionLocal", session_factory)
    second = url.replace("1234567890", "1234567891")
    class FakeFetcher:
        last_url = None
        calls = []
        def get(self, target):
            self.calls.append(target)
            if "/prodam" in target:
                self.last_url = TARGET
                first_page = "p=1" in target
                detail = url if first_page else second
                page = f'<div data-marker="item"><a data-marker="item-title" href="{detail}">Квартира</a></div>'
                return page + ('<a rel="next" href="?p=2">Далее</a>' if first_page else '')
            return html
    fetcher = FakeFetcher()
    with session_factory.begin() as session:
        run_id = create_run(session).id
    collect(run_id, threading.Event(), Settings(), fetcher)
    assert fetcher.calls == [CATALOG_URL + "?p=1", url, TARGET + "&p=2", second]
    with session_factory() as session:
        run = session.get(CollectionRun, run_id)
        assert run.status == "completed" and run.pages == 2 and run.saved == 2
