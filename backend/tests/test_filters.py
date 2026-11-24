from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.models import City, Complex, FeatureValue, Listing, PriceObservation
from app.repository import save_listing

IDS = ['9100000001', '9100000002', '9100000003', '9100000004']



def matched(client, params):
    response = client.get('/api/listings', params=params)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['total_all'] == 4
    assert body['total'] == len(body['items'])
    return sorted(row['avito_id'] for row in body['items'])


@pytest.mark.parametrize('params,indexes', [
    ({'market_type': 'secondary'}, [0, 2]), ({'market_type': 'new'}, [1, 3]), ({'market_type': 'all'}, [0, 1, 2, 3]),
    ({'rooms': '0'}, [0]), ({'rooms': '2'}, [1]), ({'rooms': 'open_plan'}, [3]),
    ([('rooms', '0'), ('rooms', 'open_plan')], [0, 3]),
    ({'total_area_min': 25, 'total_area_max': 50}, [0, 1]),
    ({'total_area_min': 75}, [2, 3]), ({'total_area_max': 25}, [0]),
    ({'floor_min': 8, 'floor_max': 10}, [1, 2]), ({'floor_min': 2, 'floor_max': 2}, [0]),
    ({'floors_total_min': 10, 'floors_total_max': 10}, [0, 2]),
    ({'ceiling_height_min': '2.8', 'ceiling_height_max': '3'}, [1, 2]),
    ({'balcony': 'present'}, [0, 1]), ({'balcony': 'balcony'}, [0]), ({'balcony': 'loggia'}, [1]),
    ({'balcony': 'absent'}, [2]), ({'balcony': 'unknown'}, [3]),
    ({'bathroom': 'combined'}, [0, 2]), ({'bathroom': 'separate'}, [1, 2]),
    ([('bathroom', 'combined'), ('bathroom', 'separate')], [0, 1, 2]),
    ({'finish': 'предчистовая'}, [1]),
    ([('finish', 'без отделки'), ('finish', 'предчистовая')], [1]),
    ([('finish', 'КОСМЕТИЧЕСКИЙ'), ('finish', 'евро')], [0, 2]),
    ({'price_min': 2_500_000, 'price_max': 8_000_000}, [0, 1]),
    ({'price_per_sqm_min': 100_000, 'price_per_sqm_max': 120_000}, [0, 2]),
    ({'price_per_sqm_min': 150_000}, [1, 3]), ({'price_max': 2_499_999}, []),
])
def test_individual_filters_and_inclusive_bounds(client, params, indexes):
    assert matched(client, params) == [IDS[index] for index in indexes]


def test_all_filters_combine_before_sorting_pagination_and_count(client):
    options = client.get('/api/listings/filter-options').json()
    complex_id = next(row['id'] for row in options['complexes'] if row['name'] == 'ЖК Бета')
    params = {'market_type': 'new', 'rooms': 2, 'complex_id': complex_id, 'total_area_min': 50, 'total_area_max': 50,
              'floor_min': 8, 'floor_max': 8, 'floors_total_min': 20, 'floors_total_max': 20,
              'ceiling_height_min': 3, 'ceiling_height_max': 3, 'balcony': 'loggia', 'bathroom': 'separate',
              'finish': 'без отделки', 'price_min': 8_000_000, 'price_max': 8_000_000,
              'price_per_sqm_min': 160_000, 'price_per_sqm_max': 160_000, 'page_size': 1,
              'sort_by': 'price_per_sqm', 'sort_order': 'asc'}
    assert matched(client, params) == [IDS[1]]
    body = client.get('/api/listings', params={**params, 'page': 2}).json()
    assert body['items'] == [] and body['total'] == 1 and body['total_all'] == 4


def test_multiple_complexes_unknown_complex_and_global_options(client, session_factory):
    options = client.get('/api/listings/filter-options').json()
    alpha = next(row['id'] for row in options['complexes'] if row['name'] == 'ЖК Альфа')
    beta = next(row['id'] for row in options['complexes'] if row['name'] == 'ЖК Бета')
    assert matched(client, {'complex_id': alpha}) == [IDS[0], IDS[3]]
    assert matched(client, [('complex_id', alpha), ('complex_id', beta)]) == [IDS[0], IDS[1], IDS[3]]
    assert matched(client, {'complex_id': 0}) == [IDS[2]]
    assert matched(client, [('complex_id', 0), ('complex_id', beta)]) == [IDS[1], IDS[2]]
    assert matched(client, {'complex_id': 999999999}) == []
    # Unreferenced dictionary entries must not appear in the pickers.
    with session_factory.begin() as session:
        city_id = session.scalar(select(City.id))
        session.add(Complex(city_id=city_id, name='Неиспользуемый ЖК'))
        session.add(FeatureValue(type_code='finish', value='Неиспользуемая отделка'))
    current = client.get('/api/listings/filter-options').json()
    assert current == options and current['has_no_complex']
    assert current['rooms'] == [{'value': '0', 'label': 'Студия'}, {'value': '2', 'label': '2'},
                                 {'value': '3', 'label': '3'}, {'value': 'open_plan', 'label': 'Свободная планировка'}]
    assert set(current['finishes']) == {'косметический', 'без отделки', 'предчистовая', 'евро'}


@pytest.mark.parametrize('direction,expected', [('asc', [0, 2, 3, 1]), ('desc', [1, 3, 2, 0])])
def test_unit_price_is_current_price_divided_by_total_area_with_stable_pages(client, direction, expected):
    body = client.get('/api/listings', params={'sort_by': 'price_per_sqm', 'sort_order': direction}).json()
    assert [row['avito_id'] for row in body['items']] == [IDS[index] for index in expected]
    assert {row['avito_id']: row['price_per_sqm'] for row in body['items']} == dict(zip(IDS, [100000, 160000, 120000, 150000]))
    pages = [client.get('/api/listings', params={'sort_by': 'price_per_sqm', 'sort_order': direction,
             'page_size': 1, 'page': number}).json()['items'][0]['avito_id'] for number in range(1, 5)]
    assert pages == [IDS[index] for index in expected]


def test_unit_price_rounding_matches_display_filter_and_sort(client, session_factory, filter_data):
    with session_factory.begin() as session:
        save_listing(session, replace(filter_data[0], total_area=Decimal('3'), price=100),
                     observed_at=datetime(2026, 10, 17, 2, tzinfo=timezone.utc))
    assert matched(client, {'price_per_sqm_min': '33.33', 'price_per_sqm_max': '33.33'}) == [IDS[0]]
    row = client.get('/api/listings', params={'price_per_sqm_max': '33.33'}).json()['items'][0]
    assert row['price_per_sqm'] == 33.33


def test_missing_price_has_no_unit_price_and_is_excluded_from_price_bounds(client, session_factory):
    with session_factory.begin() as session:
        listing = session.scalar(select(Listing).where(Listing.avito_id == IDS[0]))
        session.execute(delete(PriceObservation).where(PriceObservation.listing_id == listing.id))
    for direction in ['asc', 'desc']:
        body = client.get('/api/listings', params={'sort_by': 'price_per_sqm', 'sort_order': direction}).json()
        assert body['items'][-1]['avito_id'] == IDS[0] and body['items'][-1]['price_per_sqm'] is None
    assert matched(client, {'price_per_sqm_min': 0}) == IDS[1:]


@pytest.mark.parametrize('params', [
    {'market_type': 'invalid'}, {'rooms': '-1'}, {'rooms': 'unknown'}, {'complex_id': '-1'},
    {'balcony': 'none'}, {'bathroom': 'invalid'}, {'price_min': '-1'}, {'price_per_sqm_min': 'NaN'},
    {'total_area_min': 'Infinity'}, {'ceiling_height_min': '-0.1'}, {'floor_min': '1.5'},
    {'total_area_min': 50, 'total_area_max': 20}, {'floor_min': 10, 'floor_max': 3},
    {'floors_total_min': 20, 'floors_total_max': 5}, {'ceiling_height_min': 3, 'ceiling_height_max': 2},
    {'price_min': 200, 'price_max': 100}, {'price_per_sqm_min': 200, 'price_per_sqm_max': 100},
])
def test_invalid_filters_are_rejected(client, params):
    assert client.get('/api/listings', params=params).status_code == 422


def test_reading_filters_and_unit_prices_does_not_change_price_history(client, session_factory):
    with session_factory() as session:
        before = session.scalar(select(func.count(PriceObservation.id)))
    client.get('/api/listings', params={'price_per_sqm_min': 0, 'sort_by': 'price_per_sqm'})
    client.get('/api/listings/filter-options')
    with session_factory() as session:
        assert session.scalar(select(func.count(PriceObservation.id))) == before == 5


def test_filter_options_use_one_snapshot_query_for_current_and_history(client, session_factory):
    from sqlalchemy import event

    with session_factory() as session:
        connection = session.connection()
        statements = []
        def count(connection, cursor, statement, *args):
            if statement.lstrip().startswith(('SELECT', 'WITH')):
                statements.append(statement)
        event.listen(connection, 'before_cursor_execute', count)
        try:
            for path in ('/api/listings/filter-options', '/api/listings/filter-options?month=2026-10'):
                statements.clear()
                response = client.get(path)
                assert response.status_code == 200, response.text
                assert response.json()['rooms']
                assert len(statements) == 1 and 'filter_scope AS MATERIALIZED' in statements[0]
        finally:
            event.remove(connection, 'before_cursor_execute', count)
