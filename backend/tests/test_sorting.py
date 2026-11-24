from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import delete, update

from app.db import get_session
from app.main import app
from app.models import Listing, PriceObservation
from app.parser import parse_listing
from app.repository import save_listing

IDS = ["9000000001", "9000000002", "9000000003"]


@pytest.fixture
def sorting_client(session_factory, html, url):
    data = parse_listing(html, url)
    start = datetime(2026, 10, 17, tzinfo=timezone.utc)
    records = [
        (10, "100", "12", "60", 2, 20, "3.1", "Чистовая", 9_000_000),
        (0, "22", "8", "12", 10, 10, "2.5", "Без отделки", 2_000_000),
        (2, "50", "10", "30", 3, 30, "2.9", "Дизайнерская", 6_000_000),
    ]
    with session_factory.begin() as session:
        for avito_id, values in zip(IDS, records):
            rooms, total, kitchen, living, floor, floors_total, ceiling, finish, price = values
            item = replace(data, avito_id=avito_id, url=url.replace(data.avito_id, avito_id), rooms=rooms,
                           total_area=Decimal(total), kitchen_area=Decimal(kitchen), living_area=Decimal(living),
                           floor=floor, floors_total=floors_total, ceiling_height=Decimal(ceiling),
                           features={"finish": [finish]}, price=price)
            save_listing(session, item, observed_at=start)
            if avito_id == IDS[0]:
                # Current price is lower than its old price and all other listings.
                save_listing(session, replace(item, price=1_000_000), observed_at=start + timedelta(hours=1))
    def override():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("sort_by,ascending", [
    ("rooms", [1, 2, 0]),
    ("total_area", [1, 2, 0]),
    ("kitchen_area", [1, 2, 0]),
    ("living_area", [1, 2, 0]),
    ("floor", [0, 2, 1]),
    ("ceiling_height", [1, 2, 0]),
    ("finish", [1, 2, 0]),
    ("price", [0, 1, 2]),
])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_sorting_applies_to_whole_database_before_pagination(sorting_client, sort_by, ascending, direction):
    expected = [IDS[index] for index in ascending]
    if direction == "desc":
        expected.reverse()
    actual = []
    for page in range(1, 4):
        response = sorting_client.get("/api/listings", params={
            "page": page, "page_size": 1, "sort_by": sort_by, "sort_order": direction})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] == 3 and body["page"] == page
        assert body["sort_by"] == sort_by and body["sort_order"] == direction
        actual.append(body["items"][0]["avito_id"])
    assert actual == expected


@pytest.mark.parametrize("sort_by", ["rooms", "kitchen_area", "living_area", "floor", "ceiling_height", "finish", "price"])
@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_missing_values_remain_last_in_both_directions(sorting_client, session_factory, html, url, sort_by, direction):
    avito_id = "9000000004"
    data = replace(parse_listing(html, url), avito_id=avito_id, url=url.replace("1234567890", avito_id),
                   rooms=None, is_open_plan=True, kitchen_area=None, living_area=None, floor=None,
                   ceiling_height=None, features={})
    with session_factory.begin() as session:
        listing, _ = save_listing(session, data)
        if sort_by == "price":
            session.execute(delete(PriceObservation).where(PriceObservation.listing_id == listing.id))
    body = sorting_client.get("/api/listings", params={"sort_by": sort_by, "sort_order": direction}).json()
    assert body["total"] == 4 and len(body["items"]) == 4
    assert body["items"][-1]["avito_id"] == avito_id


@pytest.mark.parametrize("direction", ["asc", "desc"])
def test_equal_values_have_stable_pagination(sorting_client, session_factory, direction):
    with session_factory.begin() as session:
        session.execute(update(Listing).values(rooms=2))
    def pages():
        return [sorting_client.get("/api/listings", params={"sort_by": "rooms", "sort_order": direction,
                "page_size": 1, "page": page}).json()["items"][0]["avito_id"] for page in range(1, 4)]
    assert pages() == pages() == list(reversed(IDS))


def test_multiple_finish_values_sort_as_one_listing_without_duplicates(sorting_client, session_factory, html, url):
    data = parse_listing(html, url)
    finishes = [["Чистовая", "Без отделки"], ["Без отделки", "Дизайнерская"], ["Дизайнерская"]]
    with session_factory.begin() as session:
        for avito_id, finish in zip(IDS, finishes):
            save_listing(session, replace(data, avito_id=avito_id, url=url.replace(data.avito_id, avito_id),
                                          features={"finish": finish}),
                         observed_at=datetime(2026, 10, 17, 2, tzinfo=timezone.utc))
    body = sorting_client.get("/api/listings", params={"sort_by": "finish", "sort_order": "asc"}).json()
    assert body["total"] == 3 and len(body["items"]) == 3
    assert [row["avito_id"] for row in body["items"]] == [IDS[1], IDS[0], IDS[2]]
    assert body["items"][1]["features"]["finish"] == ["Без отделки", "Чистовая"]


@pytest.mark.parametrize("params", [{"sort_by": "title; DROP TABLE listings"}, {"sort_order": "up"}])
def test_unsupported_sort_parameters_are_rejected(sorting_client, params):
    assert sorting_client.get("/api/listings", params=params).status_code == 422


def test_default_order_and_current_price_are_preserved(sorting_client):
    body = sorting_client.get("/api/listings").json()
    assert body["sort_by"] == "last_seen_at" and body["sort_order"] == "desc"
    assert [row["avito_id"] for row in body["items"]] == [IDS[0], IDS[2], IDS[1]]
    assert body["items"][0]["price"] == 1_000_000 and body["items"][0]["price_count"] == 2
