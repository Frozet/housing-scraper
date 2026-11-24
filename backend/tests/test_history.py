from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import get_session
from app.main import app
from app.models import Address, FeatureValue, Listing, PriceObservation
from app.parser import parse_listing
from app.repository import create_run, save_listing


def test_price_history_retains_each_change_and_return_to_old_price(session, html, url):
    data = parse_listing(html, url)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    changes = []
    for offset, price in enumerate([7800000, 7800000, 7600000, 7600000, 7800000]):
        listing, changed = save_listing(session, replace(data, price=price), observed_at=start + timedelta(days=offset))
        changes.append(changed)
    observations = session.scalars(select(PriceObservation).order_by(PriceObservation.id)).all()
    assert [item.amount for item in observations] == [7800000, 7600000, 7800000]
    assert [item.observed_at.day for item in observations] == [1, 3, 5]
    assert changes == [False, False, True, False, True]
    assert listing.first_seen_at == start
    assert listing.last_seen_at == start + timedelta(days=4)
    assert session.scalar(select(func.count(Listing.id))) == 1
    assert session.scalar(select(func.count(Address.id))) == 1
    assert session.scalar(select(func.count(FeatureValue.id))) == 11


def test_same_address_different_avito_ids_have_independent_history(session, html, url):
    data = parse_listing(html, url)
    first, _ = save_listing(session, data)
    second, _ = save_listing(session, replace(data, avito_id="9999999999", url=url.replace("1234567890", "9999999999"), price=9000000))
    assert first.id != second.id
    assert session.scalar(select(func.count(PriceObservation.id))) == 2
    assert session.scalar(select(func.count(Address.id))) == 1


def test_open_plan_persists_and_can_be_updated_without_losing_price_history(session, html, url):
    data = parse_listing(html.replace("Количество комнат: 2", "Количество комнат: свободная планировка"), url)
    listing, _ = save_listing(session, data)
    session.expire_all()
    stored = session.get(Listing, listing.id)
    assert stored.rooms is None and stored.is_open_plan
    save_listing(session, replace(data, rooms=2, is_open_plan=False, price=7500000))
    session.expire_all()
    stored = session.get(Listing, listing.id)
    assert stored.rooms == 2 and not stored.is_open_plan
    assert session.scalars(select(PriceObservation.amount).order_by(PriceObservation.id)).all() == [7800000, 7500000]


def test_null_rooms_require_explicit_open_plan_in_database(session_factory, html, url):
    data = replace(parse_listing(html, url), rooms=None, is_open_plan=False)
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            save_listing(session, data)


def test_same_price_updates_last_check_without_rewriting_history_timestamp(session, html, url):
    data = parse_listing(html, url)
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    listing, _ = save_listing(session, data, observed_at=start)
    save_listing(session, replace(data, source_updated_at=start + timedelta(days=2)), observed_at=start + timedelta(days=3))
    observation = session.scalar(select(PriceObservation).where(PriceObservation.listing_id == listing.id))
    assert observation.observed_at == start
    assert observation.source_updated_at == data.source_updated_at
    assert listing.source_updated_at == start + timedelta(days=2)
    assert listing.last_seen_at == start + timedelta(days=3)
    assert session.scalar(select(func.count(PriceObservation.id))) == 1


def test_failed_transaction_does_not_leave_price_without_listing(session_factory, html, url):
    with pytest.raises(RuntimeError):
        with session_factory.begin() as session:
            save_listing(session, parse_listing(html, url))
            raise RuntimeError("Сбой после записи")
    with session_factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 0
        assert session.scalar(select(func.count(PriceObservation.id))) == 0


def test_only_one_run_can_be_active(session_factory):
    with session_factory.begin() as session:
        create_run(session)
    with pytest.raises(IntegrityError):
        with session_factory.begin() as session:
            create_run(session)


def test_table_and_history_api(session_factory, html, url):
    def override():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = override
    try:
        with session_factory.begin() as session:
            data = parse_listing(html, url)
            listing, _ = save_listing(session, data)
            listing_id = listing.id
            save_listing(session, replace(data, price=7500000))
        with TestClient(app) as client:
            table = client.get("/api/listings").json()
            assert table["total"] == 1
            assert table["items"][0]["price"] == 7500000
            assert table["items"][0]["price_count"] == 2
            history = client.get(f"/api/listings/{listing_id}/prices").json()
            assert [p["change"] for p in history["items"]] == [None, -300000]
            assert client.get("/api/listings/999999/prices").status_code == 404
            assert client.get("/api/listings?page=0").status_code == 422
            assert client.post("/api/runs", headers={"Origin": "https://evil.example"}).status_code == 403
            assert client.post("/api/runs").status_code == 202
            assert client.post("/api/runs").status_code == 409
    finally:
        app.dependency_overrides.clear()
