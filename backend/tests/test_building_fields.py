import json
from dataclasses import replace
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.models import City, FeatureValue, Listing, PriceObservation
from app.parser import parse_listing
from app.repository import save_listing


def with_params(html, rows):
    section = '<div data-marker="item-view/item-params"><ul>' + ''.join(f'<li>{row}</li>' for row in rows) + '</ul></div>'
    return html.replace('</main>', section + '</main>')


def test_building_fields_in_own_house_section(html, url):
    document = with_params(html, ['Тип дома: Кирпичный', 'Год постройки: 1978 г.',
                                  'Пассажирский лифт: 2 шт.', 'Парковка: Наземная; Гостевая, наземная'])
    data = parse_listing(document, url)
    assert data.construction_year == 1978
    assert data.features['house_type'] == ['кирпичный']
    assert data.features['passenger_elevator'] == ['2']
    assert data.features['parking'] == ['наземная', 'гостевая']


@pytest.mark.parametrize('value,expected', [('нет', ['нет']), ('0', ['нет']), ('0 шт.', ['нет']), ('отсутствует', ['нет']),
    ('есть', ['есть']), ('1', ['1']), ('не указано', None), ('—', None)])
def test_unknown_elevator_is_different_from_absence_and_presence_does_not_invent_count(html, url, value, expected):
    data = parse_listing(with_params(html, ['Пассажирский лифт: ' + value]), url)
    assert data.features.get('passenger_elevator') == expected


@pytest.mark.parametrize('value,year', [('2001', 2001), ('2020 год', 2020), ('не указано', None),
    ('Сдан 2026', None), ('2025–2026', None), ('9999', None)])
def test_optional_year_never_breaks_listing_and_is_not_guessed(html, url, value, year):
    data = parse_listing(with_params(html, ['Год постройки: ' + value, 'Срок сдачи: 2026']), url)
    assert data.construction_year == year


def test_building_fields_ignore_recommended_listings(html, url):
    recommendation = '<div data-marker="item"><div data-marker="item-view/item-params"><ul>' + (
        '<li>Тип дома: панельный</li><li>Год постройки: 2010</li><li>Парковка: подземная</li>'
        '<li>Пассажирский лифт: 3</li></ul></div><dl><dt>Год постройки</dt><dd>2000</dd></dl></div>')
    data = parse_listing(html.replace('</main>', recommendation + '</main>'), url)
    assert data.construction_year is None
    assert not {'house_type', 'passenger_elevator', 'parking'} & data.features.keys()


def test_building_fields_from_schema_additional_properties(html, url):
    properties = [{'name': key, 'value': value} for key, value in
                  [('Год постройки', 1999), ('Тип дома', 'Панельный'), ('Пассажирский лифт', 0), ('Парковка', 'нет')]]
    document = html + '<script type="application/ld+json">' + json.dumps({'@type': 'Apartment', 'additionalProperty': properties}) + '</script>'
    data = parse_listing(document, url)
    assert data.construction_year == 1999
    assert data.features['house_type'] == ['панельный']
    assert data.features['passenger_elevator'] == ['нет']
    assert data.features['parking'] == ['нет']


@pytest.fixture
def building_client(client, session_factory, filter_data):
    records = [
        (1978, 'кирпичный', 'нет', ['наземная']),
        (2026, 'монолитный', '2', ['подземная', 'гостевая']),
        (2001, 'панельный', '1', ['наземная', 'гостевая']),
        (None, None, None, []),
    ]
    with session_factory.begin() as session:
        for index, (data, (year, house_type, elevator, parking)) in enumerate(zip(filter_data, records)):
            features = {**data.features, 'house_type': [house_type] if house_type else [],
                        'passenger_elevator': [elevator] if elevator else [], 'parking': parking}
            save_listing(session, replace(data, construction_year=year, features=features,
                         price=2_500_000 if index == 0 else data.price),
                         observed_at=datetime(2026, 10, 18, tzinfo=timezone.utc))
    return client


@pytest.mark.parametrize('params,indexes', [
    ({'construction_year_min': 1978, 'construction_year_max': 2001}, [0, 2]),
    ({'construction_year_min': 2026, 'construction_year_max': 2026}, [1]),
    ({'construction_year_max': 1978}, [0]),
    ({'house_type': 'КИРПИЧНЫЙ'}, [0]),
    ([('house_type', 'панельный'), ('house_type', 'монолитный')], [1, 2]),
    ({'passenger_elevator': 'нет'}, [0]), ({'passenger_elevator': '1'}, [2]),
    ([('passenger_elevator', '1'), ('passenger_elevator', '2')], [1, 2]),
    ({'parking': 'гостевая'}, [1, 2]),
    ([('parking', 'гостевая'), ('parking', 'подземная')], [1, 2]),
    ({'construction_year_min': 2000, 'house_type': 'монолитный', 'passenger_elevator': '2', 'parking': 'подземная', 'rooms': 2}, [1]),
])
def test_building_filters_are_shared_by_table_analytics_and_map(building_client, params, indexes):
    client = building_client
    expected = [str(9100000001 + index) for index in indexes]
    table = client.get('/api/listings', params=params)
    assert table.status_code == 200, table.text
    data = table.json()
    assert sorted(row['avito_id'] for row in data['items']) == expected
    assert data['total'] == len(expected) and data['total_all'] == 4
    analytics = client.get('/api/analytics', params=params)
    assert analytics.status_code == 200, analytics.text
    assert sorted(row['avito_id'] for row in analytics.json()['points']) == expected
    mapped = client.get('/api/map', params=params)
    assert mapped.status_code == 200, mapped.text
    assert mapped.json()['total'] == len(expected)


def test_building_options_and_metadata_updates_preserve_prices(building_client, session_factory):
    options = building_client.get('/api/listings/filter-options').json()
    assert options['house_types'] == ['кирпичный', 'монолитный', 'панельный']
    assert options['passenger_elevators'] == ['1', '2', 'нет']
    assert options['parking_options'] == ['гостевая', 'наземная', 'подземная']
    rows = {row['avito_id']: row for row in building_client.get('/api/listings').json()['items']}
    assert rows['9100000001']['construction_year'] == 1978
    assert rows['9100000004']['construction_year'] is None
    with session_factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 4
        assert session.scalar(select(func.count(PriceObservation.id))) == 5
        assert session.scalar(select(func.count(FeatureValue.id)).where(FeatureValue.type_code == 'parking')) == 3


def test_building_options_are_scoped_to_selected_city(building_client, session_factory):
    with session_factory.begin() as session:
        city = City(name='Пустой город', region='Тест')
        session.add(city)
        session.flush()
        city_id = city.id
        session.add(FeatureValue(type_code='house_type', value='Неиспользуемый тип'))
    options = building_client.get('/api/listings/filter-options', params={'city_id': city_id}).json()
    assert options['house_types'] == options['parking_options'] == options['passenger_elevators'] == []
    assert 'Неиспользуемый тип' not in building_client.get('/api/listings/filter-options').json()['house_types']


@pytest.mark.parametrize('params', [
    {'construction_year_min': 2020, 'construction_year_max': 1990},
    {'construction_year_min': 999}, {'construction_year_max': 3000}, {'construction_year_min': '2000.5'},
])
def test_invalid_construction_year_filters_are_rejected(client, params):
    for path in ['/api/listings', '/api/analytics', '/api/map']:
        assert client.get(path, params=params).status_code == 422
