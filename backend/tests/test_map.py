from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
import httpx
from sqlalchemy import delete, select

from app.cities import default_city
from app.geography import building_key, coordinates
from app.map_index import address_index, apply_index, fetch_buildings
from app.models import Address, City, Listing, MapBuilding, MapImport, PriceObservation
from app.parser import parse_listing
from app.repository import save_listing


@pytest.mark.parametrize('address', ['ул. Култукская, д. 14', 'ул.Култукская,д.14', 'Култукская улица, 14',
    'Иркутск, ул. Култукская, 14', 'Иркутская обл., г. Иркутск, ул. Култукская, д. 14'])
def test_house_keys_normalize_spelling(address):
    assert building_key(address, 'Иркутск') == 'култукская|14'


@pytest.mark.parametrize('address', ['ул. Ярославского, з/у 129/2', 'ЖК «Дом»',
    'ул. Пискунова/Ширямова/Советская, д. 26', 'Иркутск', 'улица Ленина'])
def test_unknown_or_ambiguous_house_is_not_guessed(address):
    assert building_key(address, 'Иркутск') is None


def test_house_suffixes_stay_distinct():
    keys = {building_key('ул. Ленина, д. ' + suffix) for suffix in ['1', '1а', '1/2', '1к2', '12']}
    assert len(keys) == 5 and None not in keys


def test_short_drive_abbreviation_matches_street_index():
    assert building_key('Иркутская обл., Усолье-Сибирское, пр. Серёгина, 30', 'Усолье-Сибирское') == 'серегина|30'


@pytest.mark.parametrize('locality', ['с. Новожилкино', 'пос. Михайловка', 'деревня Ленина', 'г. Ангарск'])
def test_address_in_another_locality_is_not_mapped_to_a_city_street(locality):
    assert building_key(f'Иркутская обл., {locality}, ул. Ленина, 16', 'Усолье-Сибирское') is None


@pytest.mark.parametrize('lat,lon', [('NaN', 104), (52, 'Infinity'), (86, 104), (52, 181), (None, None)])
def test_invalid_coordinates_are_ignored(lat, lon):
    assert coordinates(lat, lon) is None


def test_listing_uses_own_map_coordinates_and_ignores_recommendations(html, url):
    document = html + '<div data-marker="item"><div data-map-lat="55.5" data-map-lon="37.5"></div></div>'
    assert parse_listing(document, url).latitude is None
    document += '<div data-map-lat="52.28315" data-map-lon="104.32173" data-item-id="1234567890"></div>'
    data = parse_listing(document, url)
    assert (data.latitude, data.longitude) == (Decimal('52.2831500'), Decimal('104.3217300'))
    assert parse_listing(document.replace('data-item-id="1234567890"', 'data-item-id="other"'), url).latitude is None


def test_schema_coordinates_are_optional_and_read_from_current_listing(html, url):
    document = html + '<script type="application/ld+json">{"@type":"Product","geo":{"latitude":52.28,"longitude":104.3}}</script>'
    assert parse_listing(document, url).latitude == Decimal('52.2800000')
    assert parse_listing(document.replace('52.28', 'NaN'), url).latitude is None


def test_coordinate_updates_do_not_duplicate_prices_and_manual_choice_is_preserved(session, html, url):
    data = replace(parse_listing(html, url), latitude=Decimal('52.28'), longitude=Decimal('104.3'))
    listing, _ = save_listing(session, data)
    assert listing.address.coordinate_source == 'avito'
    save_listing(session, replace(data, latitude=None, longitude=None))
    assert listing.address.latitude == Decimal('52.28')
    listing.address.latitude, listing.address.coordinate_source = Decimal('52.29'), 'manual'
    save_listing(session, data)
    assert listing.address.latitude == Decimal('52.29')
    assert len(session.scalars(select(PriceObservation)).all()) == 1


def test_osm_index_rejects_distant_homonyms_and_unknown_houses():
    city = SimpleNamespace(latitude=Decimal('52.28'), longitude=Decimal('104.3'))
    elements = [
        {'tags': {'addr:street': 'улица Ленина', 'addr:housenumber': '1'}, 'lat': 52.28, 'lon': 104.3},
        {'tags': {'addr:street': 'улица Ленина', 'addr:housenumber': '1'}, 'center': {'lat': 52.4, 'lon': 104.4}},
        {'tags': {'addr:street': 'Култукская улица', 'addr:housenumber': '14'}, 'center': {'lat': 52.28315, 'lon': 104.32173}},
        {'tags': {'addr:street': 'Култукская улица', 'addr:housenumber': '14'}, 'lat': 52.28316, 'lon': 104.32173},
        {'tags': {'addr:street': 'Далёкая', 'addr:housenumber': '14'}, 'lat': 55.7, 'lon': 37.6},
    ]
    index = address_index(elements, city)
    assert list(index) == ['култукская|14']


@pytest.mark.parametrize('result', ['complete', 'incomplete', 'unavailable'])
def test_city_import_is_one_identified_bounded_request_and_does_not_accept_partial_results(monkeypatch, result):
    original_client = httpx.Client
    calls = []
    def handler(request):
        calls.append(request)
        assert request.headers['User-Agent'].startswith('HousingScraperLocal/')
        assert 'nwr' in request.content.decode() and 'area.city' in request.content.decode()
        return httpx.Response(503 if result == 'unavailable' else 200,
            json={'elements': [], **({'remark': 'runtime timeout'} if result == 'incomplete' else {})})
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(handler), **kwargs))
    city = SimpleNamespace(name='Иркутск', latitude=52.28, longitude=104.3)
    if result == 'complete':
        assert fetch_buildings(city) == {}
    else:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            fetch_buildings(city)
    assert len(calls) == 1


def test_imported_index_updates_old_addresses_and_reuses_for_new_listings(session, html, url):
    data = parse_listing(html, url)
    listing, _ = save_listing(session, data)
    city = default_city(session)
    session.add(MapBuilding(city_id=city.id, key='лермонтова|100', latitude=52.28, longitude=104.3))
    session.flush()
    apply_index(session, city)
    assert listing.address.coordinate_source == 'osm'
    session.add(MapBuilding(city_id=city.id, key='култукская|14', latitude=52.28315, longitude=104.32173))
    session.flush()
    second, _ = save_listing(session, replace(data, avito_id='9876543210', address='ул. Култукская, д. 14'))
    assert second.address.coordinate_source == 'osm'


def test_map_groups_same_house_uses_latest_prices_and_filters_before_aggregation(client, session_factory):
    with session_factory.begin() as session:
        rows = session.scalars(select(Listing).order_by(Listing.avito_id)).all()
        for row in rows:
            row.address.latitude, row.address.longitude = Decimal('52.28'), Decimal('104.3')
    data = client.get('/api/map').json()
    assert data['total'] == data['located'] == 4 and len(data['points']) == 1
    assert data['points'][0]['price_min'] == 2_500_000 and data['points'][0]['price_max'] == 15_000_000
    assert data['points'][0]['price_mean'] == 8_625_000 and data['points'][0]['priced_count'] == 4
    filtered = client.get('/api/map', params={'price_max': 8_000_000}).json()
    assert filtered['located'] == 2 and filtered['points'][0]['count'] == 2
    assert filtered['points'][0]['price_max'] == 8_000_000
    assert filtered['points'][0]['price_mean'] == 5_250_000 and filtered['points'][0]['priced_count'] == 2
    assert client.get('/api/map', params={'price_min': 10, 'price_max': 5}).status_code == 422
    assert client.get('/api/map', params={'rooms': 'open_plan'}).json()['points'][0]['count'] == 1


def test_map_city_isolation_missing_counts_and_popup_filters(client, session_factory, html, url):
    with session_factory.begin() as session:
        moscow = session.scalar(select(City).where(City.name == 'Москва'))
        item, _ = save_listing(session, replace(parse_listing(html, url), avito_id='9222222222'), city_id=moscow.id)
        item.address.latitude, item.address.longitude = Decimal('55.75'), Decimal('37.61')
        address_id, mid = item.address_id, moscow.id
    own = client.get('/api/map').json()
    assert own['located'] == 0 and own['total'] == 4 and sum(x['count'] for x in own['missing']) == 4
    other = client.get('/api/map', params={'city_id': mid}).json()
    assert other['located'] == other['total'] == 1
    assert client.get('/api/map/listings', params={'address_id': address_id}).json()['total'] == 0
    params = {'address_id': address_id, 'city_id': mid}
    assert client.get('/api/map/listings', params=params).json()['total'] == 1
    assert client.get('/api/map/listings', params={**params, 'price_max': 100}).json()['total'] == 0


def test_heat_mean_excludes_unpriced_listings_without_treating_them_as_free(client, session_factory):
    with session_factory.begin() as session:
        listing = session.scalar(select(Listing).where(Listing.avito_id == '9100000004'))
        listing.address.latitude, listing.address.longitude = Decimal('52.28'), Decimal('104.3')
        session.execute(delete(PriceObservation).where(PriceObservation.listing_id == listing.id))
    point = client.get('/api/map').json()['points'][0]
    assert point['count'] == 4 and point['priced_count'] == 3 and point['price_mean'] == 6_500_000
    with session_factory.begin() as session:
        session.execute(delete(PriceObservation))
    point = client.get('/api/map').json()['points'][0]
    assert point['count'] == 4 and point['priced_count'] == 0 and point['price_mean'] is None


def test_manual_pin_validates_city_and_persists(client, session_factory, html, url):
    with session_factory() as session:
        row = session.scalar(select(Listing))
        aid, cid = row.address_id, row.address.city_id
    point = {'latitude': 52.28, 'longitude': 104.3}
    endpoint = f'/api/map/addresses/{aid}?city_id={cid}'
    assert client.put(endpoint, json={'latitude': 100, 'longitude': 104}).status_code == 422
    assert client.put(endpoint, json={'latitude': 55.75, 'longitude': 37.6}).status_code == 422
    assert client.put(endpoint, json=point, headers={'Origin': 'https://other.example'}).status_code == 403
    assert client.put(endpoint, json=point).status_code == 200
    assert client.get('/api/map').json()['located'] == 4
    with session_factory() as session:
        assert session.get(Address, aid).coordinate_source == 'manual'


def test_map_prepare_is_idempotent_and_does_not_start_external_calls_in_tests(client, session_factory):
    city_id = client.get('/api/cities').json()['default_city_id']
    for _ in range(2):
        response = client.post('/api/map/prepare', params={'city_id': city_id})
        assert response.status_code == 202 and response.json()['status'] == 'queued'
    with session_factory.begin() as session:
        assert len(session.scalars(select(MapImport)).all()) == 1
        session.get(MapImport, city_id).status = 'completed'
    assert client.post('/api/map/prepare', params={'city_id': city_id}).json()['status'] == 'completed'


def test_large_marker_can_load_address_group_over_500(client, session_factory):
    with session_factory() as session:
        address_id = session.scalar(select(Listing.address_id).limit(1))
    ids = [address_id, *range(100_000, 100_501)]
    response = client.post('/api/map/listings', json={'address_id': ids, 'price_max': 8_000_000})
    assert response.status_code == 200, response.text
    assert response.json()['total'] == 2


def test_overpass_stream_stops_at_the_byte_limit(monkeypatch):
    import app.map_index as map_index
    original_client = httpx.Client
    monkeypatch.setattr(map_index, 'MAX_MAP_BYTES', 128)
    monkeypatch.setattr(httpx, 'Client', lambda **kwargs: original_client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b' ' * 512)), **kwargs))
    city = SimpleNamespace(name='Иркутск', latitude=52.28, longitude=104.3)
    with pytest.raises(ValueError, match='слишком большой'):
        fetch_buildings(city)


def test_map_database_failure_is_permanent_and_connection_failure_backs_off(session):
    from sqlalchemy.exc import IntegrityError, OperationalError
    from app.map_index import map_failure
    from app.models import MapImport, utcnow

    city = default_city(session)
    job = MapImport(city_id=city.id, status='running', attempts=1)
    session.add(job)
    session.flush()
    map_failure(session, city.id, OperationalError(None, None, RuntimeError()))
    assert job.status == 'queued' and job.retry_at > utcnow()
    job.status, job.attempts = 'running', 2
    map_failure(session, city.id, IntegrityError(None, None, RuntimeError()))
    assert job.status == 'failed' and job.retry_at is None and job.finished_at
