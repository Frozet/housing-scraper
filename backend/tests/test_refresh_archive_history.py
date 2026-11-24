from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from app.cities import selected_city
from app.listing_history import archive_listing, available_months, month_cutoff
from app.models import City, CollectionRun, Crawl, CrawlItem, Listing, ListingState, PriceObservation
from app.parser import parse_listing
from app.repository import save_listing
from test_browser_api import browser_client, HEADERS, TOKEN

LOCAL = ZoneInfo('Asia/Irkutsk')


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=LOCAL)


def seed(session, html, source_url, **changes):
    data = replace(parse_listing(html, source_url), **changes)
    listing, _ = save_listing(session, data)
    return listing, data


def claim_refresh(client):
    response = client.post('/api/runs', json={'mode': 'new', 'collection_mode': 'refresh'})
    assert response.status_code == 202, response.text
    run_id = response.json()['id']
    assert client.post('/api/browser/claim', headers=HEADERS,
        json={'token': TOKEN, 'protocol_version': 4}).status_code == 200
    return run_id, f'/api/browser/runs/{run_id}'


def task(client, path):
    response = client.post(path + '/next', headers=HEADERS, json={'token': TOKEN})
    assert response.status_code == 200, response.text
    return response.json()


def test_refresh_only_queues_existing_active_city_listings_and_never_catalogs(browser_client, session_factory, html, url):
    with session_factory.begin() as session:
        first, _ = seed(session, html, url)
        closed, _ = seed(session, html, url, avito_id='9900001002', url=url.rsplit('_', 1)[0] + '_9900001002')
        archive_listing(session, closed.avito_id, 'Закрыто')
        moscow = session.scalar(select(City).where(City.name == 'Москва'))
        save_listing(session, replace(parse_listing(html, url), avito_id='9900001003'), city_id=moscow.id)
        first_id = first.id
    run_id, path = claim_refresh(browser_client)
    work = task(browser_client, path)
    assert work == {'kind': 'listing', 'url': url}
    response = browser_client.post(path + '/listing', headers=HEADERS,
        json={'token': TOKEN, 'url': url, 'html': html.replace('7800000', '7900000')})
    assert response.status_code == 200 and response.json()['price_changed']
    assert task(browser_client, path)['kind'] == 'done'
    with session_factory() as session:
        run = session.get(CollectionRun, run_id)
        assert session.get(Crawl, run.crawl_id).mode == 'refresh'
        assert run.saved == run.found == run.price_changes == 1 and run.pages == 0
        assert session.scalar(select(func.count(CrawlItem.avito_id)).where(CrawlItem.crawl_id == run.crawl_id)) == 1
        assert session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.listing_id == first_id)) == 2


def test_empty_refresh_finishes_without_opening_avito(browser_client):
    _, path = claim_refresh(browser_client)
    assert task(browser_client, path)['kind'] == 'done'


def test_refresh_merges_unknown_fields_but_accepts_explicit_changes(session, html, url):
    listing, data = seed(session, html, url, construction_year=1999)
    save_listing(session, replace(data, construction_year=None, kitchen_area=None, complex_name=None,
        features={'finish': ['евро'], 'parking': ['подземная']}), merge_missing=True)
    assert listing.construction_year == 1999 and listing.kitchen_area == Decimal('10.2') and listing.complex_id
    assert {(value.type_code, value.value) for value in listing.features} >= {
        ('finish', 'евро'), ('parking', 'подземная'), ('balcony', 'лоджия')}
    assert not any(value.value == 'чистовая' for value in listing.features)
    assert session.scalar(select(func.count(PriceObservation.id))) == 1
    assert session.scalar(select(func.count(ListingState.id))) == 2


def test_unchanged_refresh_does_not_duplicate_snapshots_or_price_history(session, html, url):
    listing, data = seed(session, html, url)
    save_listing(session, data, merge_missing=True)
    assert session.scalar(select(func.count(ListingState.id)).where(ListingState.listing_id == listing.id)) == 1
    assert session.scalar(select(func.count(PriceObservation.id))) == 1


@pytest.mark.parametrize('kind', ['closed', 'expired', 'rejected', 'deleted', 'blocked', '404', '410'])
def test_closed_refresh_archives_existing_listing_without_deleting_prices(browser_client, session_factory, html, url, request, kind):
    with session_factory.begin() as session:
        listing, _ = seed(session, html, url)
        listing_id = listing.id
    run_id, path = claim_refresh(browser_client)
    assert task(browser_client, path)['url'] == url
    suffix = '/listing' if kind in {'closed', 'expired', 'rejected', 'deleted', 'blocked'} else '/unavailable'
    body = {'token': TOKEN, 'url': url}
    body.update({'html': request.getfixturevalue(kind + '_listing')[0]}
                if kind in {'closed', 'expired', 'rejected', 'deleted', 'blocked'} else {'status': int(kind), 'actual_url': url})
    for _ in range(2):
        assert browser_client.post(path + suffix, headers=HEADERS, json=body).status_code == 200
    assert task(browser_client, path)['kind'] == 'done'
    with session_factory() as session:
        row = session.get(Listing, listing_id)
        assert row.is_archived and row.archived_at and row.archive_reason
        if kind == 'rejected':
            assert 'отклонено' in row.archive_reason
        if kind == 'deleted':
            assert 'удалено пользователем' in row.archive_reason
        if kind == 'blocked':
            assert 'заблокировано Авито после проверки' in row.archive_reason
        assert session.get(CollectionRun, run_id).archived == 1
        assert session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.listing_id == listing_id)) == 1
        assert session.scalar(select(func.count(ListingState.id)).where(ListingState.listing_id == listing_id)) == 2


@pytest.mark.parametrize('unreadable', [False, True])
def test_review_or_unreadable_listing_does_not_archive_or_overwrite_price(browser_client, session_factory, html, url, review_listing, unreadable):
    with session_factory.begin() as session:
        listing, _ = seed(session, html, url)
        listing_id = listing.id
    _, path = claim_refresh(browser_client)
    assert task(browser_client, path)['url'] == url
    response = browser_client.post(path + '/listing', headers=HEADERS,
        json={'token': TOKEN, 'url': url, 'html': '<h1>Не удалось загрузить страницу</h1>' if unreadable else review_listing[0]})
    assert response.status_code == 200 and not response.json()['saved']
    with session_factory() as session:
        assert not session.get(Listing, listing_id).is_archived
        assert session.scalar(select(func.count(PriceObservation.id))) == 1


def test_refresh_resume_keeps_queue_and_never_reopens_saved_records(browser_client, session_factory, html, url):
    other_url = url.rsplit('_', 1)[0] + '_9900002002'
    with session_factory.begin() as session:
        seed(session, html, url)
        seed(session, html, url, avito_id='9900002002', url=other_url)
    run_id, path = claim_refresh(browser_client)
    assert task(browser_client, path)['url'] == url
    browser_client.post(path + '/listing', headers=HEADERS, json={'token': TOKEN, 'url': url, 'html': html})
    browser_client.post(path + '/finish', headers=HEADERS, json={'token': TOKEN, 'status': 'interrupted'})
    resumed = browser_client.post('/api/runs', json={'mode': 'resume', 'collection_mode': 'refresh'}).json()['id']
    browser_client.post('/api/browser/claim', headers=HEADERS, json={'token': TOKEN, 'protocol_version': 4})
    assert task(browser_client, f'/api/browser/runs/{resumed}')['url'] == other_url
    with session_factory() as session:
        assert session.get(CollectionRun, run_id).crawl_id == session.get(CollectionRun, resumed).crawl_id


def test_archive_filter_is_shared_by_every_section_and_price_history_stays_accessible(client, session_factory):
    with session_factory.begin() as session:
        archive_listing(session, '9100000001', 'Закрыто')
        listing_id = session.scalar(select(Listing.id).where(Listing.avito_id == '9100000001'))
        address_id = session.get(Listing, listing_id).address_id
    for status, count in [('active', 3), ('archived', 1), ('all', 4)]:
        params = {'listing_status': status}
        for endpoint in ['/api/listings', '/api/analytics', '/api/map', '/api/map/listings']:
            response = client.get(endpoint, params={**params, **({'address_id': address_id} if endpoint.endswith('/map/listings') else {})})
            assert response.status_code == 200, response.text
            assert response.json()['total'] == count
    assert client.get('/api/listings').json()['total'] == 3
    assert len(client.get(f'/api/listings/{listing_id}/prices').json()['items']) == 2
    assert client.get('/api/listings', params={'listing_status': 'wrong'}).status_code == 422


@pytest.fixture
def temporal(client, session_factory, html, url):
    data = replace(parse_listing(html, url), avito_id='9900003001', url=url.replace('/irkutsk/', '/moskva/').replace('1234567890', '9900003001'),
                   address='Москва, ул. Ленина, 1', source_updated_at=None)
    with session_factory.begin() as session:
        city = session.scalar(select(City).where(City.name == 'Москва'))
        row, _ = save_listing(session, data, city_id=city.id, observed_at=at('2026-01-15T12:00'))
        changed = replace(data, total_area=Decimal('60'), price=8_400_000, complex_name='Новый ЖК',
                          features={**data.features, 'finish': ['евро']})
        save_listing(session, changed, city_id=city.id, observed_at=at('2026-02-10T12:00'))
        archive_listing(session, data.avito_id, 'Закрыто', at('2026-03-05T12:00'))
        save_listing(session, replace(changed, price=8_200_000), city_id=city.id, observed_at=at('2026-04-05T12:00'))
        return city.id, row.id


@pytest.mark.parametrize('month,status,count,price,area', [
    ('2026-01', 'active', 1, 7_800_000, 54.6), ('2026-02', 'active', 1, 8_400_000, 60),
    ('2026-03', 'active', 0, None, None), ('2026-03', 'archived', 1, 8_400_000, 60),
    ('2026-04', 'active', 1, 8_200_000, 60), ('2026-04', 'archived', 0, None, None),
    ('2025-12', 'all', 0, None, None),
])
def test_month_uses_historical_price_area_and_archive_status(client, temporal, month, status, count, price, area):
    city_id, _ = temporal
    response = client.get('/api/analytics', params={'city_id': city_id, 'month': month, 'listing_status': status})
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['total'] == count
    assert data['metrics']['price']['mean'] == price and data['metrics']['total_area']['mean'] == area
    assert data['period']['month'] == month and data['period']['complete']
    if count:
        assert data['points'][0]['price_per_sqm'] == round(price / area, 2)


def test_past_filters_and_options_use_past_features_and_complexes(client, temporal):
    city_id, _ = temporal
    params = {'city_id': city_id, 'month': '2026-01', 'finish': 'чистовая'}
    assert client.get('/api/analytics', params=params).json()['total'] == 1
    assert client.get('/api/analytics', params={**params, 'finish': 'евро'}).json()['total'] == 0
    options = client.get('/api/listings/filter-options', params=params).json()
    assert options['finishes'] == ['чистовая'] and options['complexes'][0]['name'] == 'Академический'
    later = client.get('/api/listings/filter-options', params={'city_id': city_id, 'month': '2026-02'}).json()
    assert later['finishes'] == ['евро'] and later['complexes'][0]['name'] == 'Новый ЖК'


def test_month_boundary_is_exclusive_in_irkutsk_time(client, session_factory, html, url):
    data = replace(parse_listing(html, url), avito_id='9900004001', source_updated_at=None)
    with session_factory.begin() as session:
        city = session.scalar(select(City).where(City.name == 'Москва'))
        save_listing(session, data, city_id=city.id, observed_at=at('2026-01-31T23:59:59.999999'))
        save_listing(session, replace(data, price=8_000_000, total_area=Decimal('70')), city_id=city.id,
                     observed_at=at('2026-02-01T00:00'))
        city_id = city.id
    data = client.get('/api/analytics', params={'city_id': city_id, 'month': '2026-01'}).json()
    assert data['total'] == 1 and data['metrics']['price']['mean'] == 7_800_000 and data['metrics']['total_area']['mean'] == 54.6
    assert month_cutoff('2024-02') == at('2024-03-01T00:00')


def test_available_months_follow_scope_at_each_month_end(session, html, url):
    data = parse_listing(html, url)
    irkutsk = selected_city(session)
    moscow = session.scalar(select(City).where(City.name == 'Москва'))
    save_listing(session, data, city_id=irkutsk.id, observed_at=at('2025-12-31T23:59:59.999999'))
    save_listing(session, replace(data, address='Москва, ул. Ленина, 1'),
                 city_id=moscow.id, observed_at=at('2026-01-01T00:00'))
    archive_listing(session, data.avito_id, 'Закрыто', at('2026-03-02T00:00'))
    now = at('2026-03-03T00:00')
    assert available_months(session, irkutsk.id, 'apartment', now) == ['2025-12']
    assert available_months(session, moscow.id, 'apartment', now) == ['2026-03', '2026-02', '2026-01']
    assert available_months(session, moscow.id, 'house', now) == []


def test_analytics_months_endpoint_lists_periods_for_the_selected_scope(client, temporal):
    city_id, _ = temporal
    response = client.get('/api/analytics/months', params={'city_id': city_id})
    assert response.status_code == 200, response.text
    months = response.json()['months']
    assert months[-4:] == ['2026-04', '2026-03', '2026-02', '2026-01']
    assert months[0] == datetime.now(LOCAL).strftime('%Y-%m')
    assert client.get('/api/analytics/months', params={'city_id': city_id, 'property_kind': 'house'}).json() == {'months': []}


def test_price_only_change_keeps_attribute_snapshot_but_changes_monthly_price(client, session_factory, html, url):
    data = replace(parse_listing(html, url), avito_id='9900005001', source_updated_at=None)
    with session_factory.begin() as session:
        city = session.scalar(select(City).where(City.name == 'Москва'))
        row, _ = save_listing(session, data, city_id=city.id, observed_at=at('2026-01-10T12:00'))
        save_listing(session, replace(data, price=8_000_000), city_id=city.id, observed_at=at('2026-02-10T12:00'))
        assert session.scalar(select(func.count(ListingState.id)).where(ListingState.listing_id == row.id)) == 1
        city_id = city.id
    for month, price in [('2026-01', 7_800_000), ('2026-02', 8_000_000)]:
        data = client.get('/api/analytics', params={'city_id': city_id, 'month': month}).json()
        assert data['metrics']['price']['mean'] == price


def test_partial_floor_update_does_not_combine_incompatible_old_and_new_values(session, html, url):
    listing, data = seed(session, html, url)
    save_listing(session, replace(data, floor=18, floors_total=None), merge_missing=True)
    assert listing.floor == 18 and listing.floors_total is None


@pytest.mark.parametrize('month', ['2026-13', '2026-00', '2026-1', '2026-02-01', '0000-01', '9999-01'])
def test_invalid_or_future_months_are_rejected(client, month):
    assert client.get('/api/analytics', params={'month': month}).status_code == 422


def test_reactivation_preserves_archive_history_and_prior_prices(client, session_factory, temporal):
    _, listing_id = temporal
    with session_factory() as session:
        row = session.get(Listing, listing_id)
        assert not row.is_archived and row.archived_at is None
        assert session.scalar(select(func.count(ListingState.id)).where(ListingState.listing_id == listing_id)) == 4
        assert session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.listing_id == listing_id)) == 3


def test_late_older_snapshot_cannot_rewind_last_seen_on_another_connection(migrate_test_database, html, url):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from uuid import uuid4
    from sqlalchemy import delete
    from sqlalchemy.orm import Session
    from app.db import engine
    from app.repository import StaleObservation

    avito_id = str(8_000_000_000_000 + uuid4().int % 1_000_000_000_000)
    source_url = url.rsplit('_', 1)[0] + '_' + avito_id
    data = replace(parse_listing(html, source_url), address=f'Иркутск, Тестовая улица, {avito_id}')
    newer, older = at('2026-10-15T12:00'), at('2026-10-15T11:00')
    started = Event()

    def late_worker():
        with Session(engine) as session, session.begin():
            session.connection()
            started.set()
            save_listing(session, data, observed_at=older)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool, Session(engine) as writer:
            save_listing(writer, data, observed_at=newer)
            delayed = pool.submit(late_worker)
            try:
                assert started.wait(5)
                writer.commit()
            finally:
                writer.rollback()
            with pytest.raises(StaleObservation):
                delayed.result(timeout=5)
        with Session(engine) as session:
            listing = session.scalar(select(Listing).where(Listing.avito_id == avito_id))
            assert listing.last_seen_at == newer
            assert session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.listing_id == listing.id)) == 1
    finally:
        from app.models import Address
        with Session(engine) as session, session.begin():
            session.execute(delete(Listing).where(Listing.avito_id == avito_id))
            session.execute(delete(Address).where(Address.text == data.address))
