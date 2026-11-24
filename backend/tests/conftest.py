import os
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import sessionmaker

# Integration tests must never operate on the application's working database.
os.environ["WORKER_ENABLED"] = "false"
os.environ["AVITO_PROXY_REQUIRED"] = "false"
os.environ["AVITO_PROXY_URL"] = ""
os.environ["COLLECTOR_MODE"] = "http"
from app.db import engine, get_session
from app.main import app
from app.parser import parse_listing
from app.repository import save_listing


def pytest_collection_modifyitems(items):
    for item in items:
        if "migrate_test_database" in item.fixturenames:
            item.add_marker(pytest.mark.db)


@pytest.fixture(scope="session")
def migrate_test_database():
    if not engine.url.database or not engine.url.database.endswith("_test"):
        pytest.exit("Для тестов задайте DATABASE_URL с отдельной базой, имя которой заканчивается на _test.")
    command.upgrade(Config("alembic.ini"), "head")


@pytest.fixture
def session_factory(migrate_test_database):
    with engine.connect() as connection:
        transaction = connection.begin()
        factory = sessionmaker(bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint")
        yield factory
        transaction.rollback()


@pytest.fixture
def session(session_factory):
    with session_factory() as session:
        yield session


@pytest.fixture
def other_city_catalog():
    return (Path(__file__).parent / "fixtures/catalog_other_cities.html").read_text(encoding="utf-8")


@pytest.fixture
def html():
    return (Path(__file__).parent / "fixtures/listing.html").read_text(encoding="utf-8")


@pytest.fixture
def hashed_listing():
    html = (Path(__file__).parent / "fixtures/listing_hashed_layout.html").read_text(encoding="utf-8")
    url = "https://www.avito.ru/irkutsk/kvartiry/3-k._kvartira_886_m_1819_et._9990000101"
    return html, url


@pytest.fixture
def november_listing():
    html = (Path(__file__).parent / "fixtures/listing_2026_11.html").read_text(encoding="utf-8")
    url = "https://www.avito.ru/irkutsk/kvartiry/1-k._kvartira_387_m_411_et._9990000102"
    return html, url


@pytest.fixture
def review_listing():
    html = (Path(__file__).parent / "fixtures/listing_on_review.html").read_text(encoding="utf-8")
    url = "https://www.avito.ru/irkutsk/kvartiry/2-k._kvartira_43_m_25_et._8373071860"
    return html, url


@pytest.fixture
def closed_listing():
    html = (Path(__file__).parent / "fixtures/listing_closed.html").read_text(encoding="utf-8")
    url = "https://www.avito.ru/irkutsk/kvartiry/4-k._kvartira_95_m_1819_et._8367691064"
    return html, url


@pytest.fixture
def expired_listing(url):
    html = (Path(__file__).parent / "fixtures/listing_expired.html").read_text(encoding="utf-8")
    return html, url


@pytest.fixture
def rejected_listing(url):
    html = (Path(__file__).parent / "fixtures/listing_rejected.html").read_text(encoding="utf-8")
    return html, url


@pytest.fixture
def deleted_listing(url):
    html = (Path(__file__).parent / "fixtures/listing_deleted.html").read_text(encoding="utf-8")
    return html, url


@pytest.fixture
def blocked_listing(url):
    html = (Path(__file__).parent / "fixtures/listing_blocked.html").read_text(encoding="utf-8")
    return html, url


@pytest.fixture
def auction_listing():
    html = (Path(__file__).parent / "fixtures/listing_auction.html").read_text(encoding="utf-8")
    url = "https://www.avito.ru/irkutsk/kvartiry/auktsion_kvartira-studiya_225_m_810_et._7974409266"
    return html, url


@pytest.fixture
def url():
    return "https://www.avito.ru/irkutsk/kvartiry/2-k._kvartira_546_m_510_et._1234567890"


@pytest.fixture
def filter_data(session_factory, html, url):
    base = parse_listing(html, url)
    at = datetime(2026, 10, 17, tzinfo=timezone.utc)
    records = [
        dict(market_type_code='secondary', complex_name='ЖК Альфа', rooms=0, is_open_plan=False,
             total_area=Decimal('25'), floor=2, floors_total=10, ceiling_height=Decimal('2.5'), price=3_000_000,
             features={'balcony': ['балкон'], 'bathroom': ['совмещенный'], 'finish': ['косметический']}),
        dict(market_type_code='new', complex_name='ЖК Бета', rooms=2, is_open_plan=False,
             total_area=Decimal('50'), floor=8, floors_total=20, ceiling_height=Decimal('3'), price=8_000_000,
             features={'balcony': ['лоджия'], 'bathroom': ['раздельный'], 'finish': ['без отделки', 'предчистовая']}),
        dict(market_type_code='secondary', complex_name=None, rooms=3, is_open_plan=False,
             total_area=Decimal('75'), floor=10, floors_total=10, ceiling_height=Decimal('2.8'), price=9_000_000,
             features={'balcony': ['нет'], 'bathroom': ['совмещённый', 'раздельный'], 'finish': ['евро']}),
        dict(market_type_code='new', complex_name='ЖК Альфа', rooms=None, is_open_plan=True,
             total_area=Decimal('100'), floor=None, floors_total=None, ceiling_height=None, price=15_000_000, features={}),
    ]
    data = []
    with session_factory.begin() as session:
        for avito_id, record in zip(["9100000001", "9100000002", "9100000003", "9100000004"], records):
            item = replace(base, avito_id=avito_id, url=url.replace(base.avito_id, avito_id), **record)
            save_listing(session, item, observed_at=at)
            data.append(item)
        # The first listing's current price is 100,000 per m², old price was 120,000.
        save_listing(session, replace(data[0], price=2_500_000), observed_at=at + timedelta(hours=1))
    return data


@pytest.fixture
def client(session_factory, filter_data):
    def override():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()
