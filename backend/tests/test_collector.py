import threading

import pytest
from sqlalchemy import func, select

from app import collector
from app.collector import Blocked, collect
from app.config import Settings
from app.models import CollectionRun, Listing, PriceObservation
from app.repository import create_run


def test_block_after_first_listing_keeps_committed_data(session_factory, monkeypatch, html, url):
    monkeypatch.setattr(collector, "SessionLocal", session_factory)
    second = url.replace("1234567890", "1234567891")
    catalog = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{link}">Квартира</a></div>' for link in (url, second))
    class FakeFetcher:
        def get(self, target):
            if "?p=" in target:
                return catalog
            if target == url:
                return html
            raise Blocked("Доступ ограничен (HTTP 429).")
    with session_factory.begin() as session:
        run_id = create_run(session).id
    collect(run_id, threading.Event(), Settings(), FakeFetcher())
    with session_factory() as session:
        run = session.get(CollectionRun, run_id)
        assert run.status == "blocked" and run.saved == 1 and run.found == 2
        assert session.scalar(select(func.count(Listing.id))) == 1
        assert session.scalar(select(func.count(PriceObservation.id))) == 1


def test_fetcher_does_not_retry_access_blocks(monkeypatch):
    import httpx
    fetcher = collector.Fetcher(Settings(request_delay_seconds=1), threading.Event())
    calls = []
    monkeypatch.setattr(fetcher, "pause", lambda seconds: None)
    def get(url):
        calls.append(url)
        return httpx.Response(429)
    monkeypatch.setattr(fetcher.client, "get", get)
    try:
        import pytest
        with pytest.raises(Blocked):
            fetcher.get(collector.CATALOG_URL)
        assert len(calls) == 1
    finally:
        fetcher.close()


@pytest.mark.parametrize("fixture_name", ["review_listing", "closed_listing", "auction_listing"])
def test_http_collector_skips_unavailable_listing_and_continues(session_factory, monkeypatch, html, request, fixture_name):
    monkeypatch.setattr(collector, "SessionLocal", session_factory)
    review_html, url = request.getfixturevalue(fixture_name)
    prefix, avito_id = url.rsplit("_", 1)
    second = f"{prefix}_{int(avito_id) + 1}"
    catalog = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                      for u in [url, second])
    class FakeFetcher:
        def get(self, target):
            return catalog if "?p=" in target else review_html if target == url else html
    with session_factory.begin() as session:
        run_id = create_run(session).id
    collect(run_id, threading.Event(), Settings(), FakeFetcher())
    with session_factory() as session:
        run = session.get(CollectionRun, run_id)
        assert run.status == "completed" and run.saved == 1 and run.errors == 0
        assert session.scalar(select(func.count(Listing.id))) == 1


def test_http_run_status_is_visible_in_irkutsk_apartments(session_factory, monkeypatch, html, url):
    from fastapi.testclient import TestClient
    from app.cities import default_city
    from app.db import get_session
    from app.main import app

    monkeypatch.setattr(collector, 'SessionLocal', session_factory)
    with session_factory.begin() as session:
        city_id = default_city(session).id
    class Page:
        def get(self, target):
            return html if target == url else f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    def override():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            run_id = client.post('/api/runs', json={'city_id': city_id}).json()['id']
            params = {'city_id': city_id, 'property_kind': 'apartment'}
            queued = client.get('/api/collection-status', params=params).json()
            assert queued['run']['status'] == 'queued'
            collect(run_id, threading.Event(), Settings(), Page())
            done = client.get('/api/collection-status', params=params).json()
            assert done['run']['status'] == 'completed' and done['run']['saved'] == 1
            assert done['data_version'] != queued['data_version']
            assert client.get('/api/listings', params=params).json()['total'] == 1
            assert client.get('/api/runs/latest', params={**params, 'property_kind': 'house'}).json() is None
    finally:
        app.dependency_overrides.clear()
