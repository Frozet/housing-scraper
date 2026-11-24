import base64
import socket
import socketserver
import threading

import httpx
import pytest

from app import check_proxy as diagnostic, collector
from app.collector import Blocked, CollectionError, Fetcher, collect, configured_proxy
from app.config import Settings
from app.models import CollectionRun
from app.parser import CATALOG_URL
from app.repository import create_run

PROXY = "http://proxy-user:proxy-secret@proxy.example:8080"


def test_proxy_secret_is_not_exposed_in_settings():
    config = Settings(avito_proxy_url=PROXY)
    assert "proxy-secret" not in repr(config)
    assert "proxy-secret" not in config.model_dump_json()
    assert configured_proxy(config) == PROXY
    assert configured_proxy(Settings(avito_proxy_url=" ")) is None


@pytest.mark.parametrize("proxy", [
    "proxy.example:8080", "ftp://proxy.example:8080", "http://proxy.example", "http://proxy.example:0",
    "http://proxy.example:70000", "http://proxy.example:not-a-port", "http://proxy.example:8080/path",
    "http://user@proxy.example:8080", "http://user:@proxy.example:8080", "http://:secret@proxy.example:8080",
    "http://user:secret@proxy.example:8080?query=secret", "http://user:secret@proxy.example:8080#secret",
])
def test_invalid_proxy_is_rejected_without_revealing_input(proxy):
    with pytest.raises(CollectionError) as error:
        configured_proxy(Settings(avito_proxy_url=proxy))
    assert "secret" not in str(error.value)
    assert proxy not in str(error.value)


@pytest.mark.parametrize("scheme", ["http", "https", "socks5", "socks5h"])
def test_all_supported_proxy_transports_can_be_created(scheme):
    fetcher = Fetcher(Settings(avito_proxy_url=f"{scheme}://user:password@proxy.example:8080"), threading.Event())
    try:
        assert fetcher.uses_proxy
    finally:
        fetcher.close()


def test_missing_required_proxy_never_creates_network_client(monkeypatch):
    def forbidden(**kwargs):
        pytest.fail("HTTP-клиент не должен создаваться без обязательного прокси")
    monkeypatch.setattr(collector.httpx, "Client", forbidden)
    with pytest.raises(CollectionError, match="Прокси не настроен"):
        Fetcher(Settings(avito_proxy_required=True), threading.Event())


def test_required_proxy_failure_finishes_database_job(session_factory, monkeypatch):
    monkeypatch.setattr(collector, "SessionLocal", session_factory)
    with session_factory.begin() as session:
        run_id = create_run(session).id
    collect(run_id, threading.Event(), Settings(avito_proxy_required=True))
    with session_factory() as session:
        run = session.get(CollectionRun, run_id)
        assert run.status == "failed" and run.finished_at is not None
        assert "Прокси не настроен" in run.message


def test_https_request_reaches_real_connect_proxy_without_direct_fallback(monkeypatch):
    requests = []
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            request = self.rfile.readline().decode("ascii").strip()
            headers = {}
            while line := self.rfile.readline():
                if line == b"\r\n":
                    break
                key, value = line.decode("ascii").split(":", 1)
                headers[key.lower()] = value.strip()
            requests.append((request, headers))
            self.wfile.write(b'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="test"\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        port = server.server_address[1]
        original_connect = socket.create_connection
        def local_proxy_only(address, *args, **kwargs):
            assert address == ("127.0.0.1", port), "Прямое подключение к внешнему сайту запрещено в этом тесте"
            return original_connect(address, *args, **kwargs)
        monkeypatch.setattr(socket, "create_connection", local_proxy_only)
        fetcher = Fetcher(Settings(avito_proxy_url=f"http://user:secret@127.0.0.1:{port}"), threading.Event())
        monkeypatch.setattr(fetcher, "pause", lambda seconds: None)
        try:
            with pytest.raises(CollectionError, match="авторизации прокси") as error:
                fetcher.get(CATALOG_URL)
            assert "secret" not in str(error.value)
            assert len(requests) == 1
            assert requests[0][0] == "CONNECT www.avito.ru:443 HTTP/1.1"
            assert requests[0][1]["proxy-authorization"] == "Basic " + base64.b64encode(b"user:secret").decode()
        finally:
            fetcher.close()
            server.shutdown()
            thread.join(timeout=2)


def test_proxy_network_errors_are_sanitized_and_never_switch_to_direct(monkeypatch):
    fetcher = Fetcher(Settings(avito_proxy_url=PROXY), threading.Event())
    calls = []
    monkeypatch.setattr(fetcher, "pause", lambda seconds: None)
    def unavailable(url):
        calls.append(url)
        raise httpx.ConnectError("proxy-secret")
    monkeypatch.setattr(fetcher.client, "get", unavailable)
    try:
        with pytest.raises(CollectionError, match="через настроенный прокси") as error:
            fetcher.get(CATALOG_URL)
        assert "proxy-secret" not in str(error.value)
        assert calls == [CATALOG_URL] * 3
        assert fetcher.uses_proxy
    finally:
        fetcher.close()


def test_http_439_is_a_terminal_refusal(monkeypatch):
    fetcher = Fetcher(Settings(), threading.Event())
    calls = []
    monkeypatch.setattr(fetcher, "pause", lambda seconds: None)
    def denied(url):
        calls.append(url)
        return httpx.Response(439)
    monkeypatch.setattr(fetcher.client, "get", denied)
    try:
        with pytest.raises(Blocked, match="439"):
            fetcher.get(CATALOG_URL)
        assert len(calls) == 1
    finally:
        fetcher.close()


@pytest.mark.parametrize("country,expected", [("RU", True), ("DE", False)])
def test_proxy_diagnostic_checks_country_before_catalog(monkeypatch, url, country, expected):
    calls = []
    class FakeFetcher:
        def __init__(self, config, stop):
            self.client = self
        def get(self, target):
            calls.append(target)
            if target == diagnostic.COUNTRY_URL:
                return httpx.Response(200, request=httpx.Request("GET", target),
                                      json={"ip": "203.0.113.42", "country": country})
            return f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
        def close(self):
            pass
    monkeypatch.setattr(diagnostic, "Fetcher", FakeFetcher)
    result = diagnostic.check_proxy(Settings(avito_proxy_url=PROXY))
    assert result["ok"] is expected
    assert result["country"] == country
    assert result["exit_ip"] == "203.0.113.42"
    assert len(calls) == (2 if expected else 1)
    assert "proxy-secret" not in str(result)


def test_diagnostic_requires_explicit_proxy():
    result = diagnostic.check_proxy(Settings())
    assert result["ok"] is False and result["proxy_configured"] is False
