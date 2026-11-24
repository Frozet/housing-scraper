from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.analytics import summarize
from app.models import Listing, PriceObservation


@pytest.mark.parametrize("values,mean,median,modes,frequency", [
    ([], None, None, [], 0),
    ([7], "7.00", "7.00", [7], 1),
    ([1, 3, 8], "4.00", "3.00", [], 0),
    ([1, 2, 3, 10], "4.00", "2.50", [], 0),
    ([4, 1, 4, 8, 8], "5.00", "4.00", [4, 8], 2),
    ([None, 9, 9, 9], "9.00", "9.00", [9], 3),
    ([Decimal("0.01"), Decimal("0.02")], "0.02", "0.02", [], 0),
    ([Decimal("33.33")] * 3, "33.33", "33.33", [Decimal("33.33")], 3),
])
def test_decimal_statistics(values, mean, median, modes, frequency):
    result = summarize(values)
    assert result == {"count": sum(value is not None for value in values),
                      "mean": Decimal(mean) if mean is not None else None,
                      "median": Decimal(median) if median is not None else None,
                      "modes": modes, "mode_frequency": frequency}


def test_current_prices_and_all_three_metrics(client, session_factory):
    with session_factory() as session:
        before = session.scalar(select(func.count(PriceObservation.id)))
    response = client.get('/api/analytics')
    assert response.status_code == 200, response.text
    data = response.json()
    assert data['total'] == data['priced_count'] == 4
    assert data['metrics'] == {
        'price': {'count': 4, 'mean': 8_625_000, 'median': 8_500_000, 'modes': [], 'mode_frequency': 0},
        'price_per_sqm': {'count': 4, 'mean': 132_500, 'median': 135_000, 'modes': [], 'mode_frequency': 0},
        'total_area': {'count': 4, 'mean': 62.5, 'median': 62.5, 'modes': [], 'mode_frequency': 0},
    }
    assert [point['price'] for point in data['points']] == [2_500_000, 8_000_000, 9_000_000, 15_000_000]
    assert [point['price_per_sqm'] for point in data['points']] == [100_000, 160_000, 120_000, 150_000]
    assert datetime.fromisoformat(data['calculated_at']).tzinfo is not None
    with session_factory() as session:
        assert session.scalar(select(func.count(PriceObservation.id))) == before == 5


@pytest.mark.parametrize('params,ids', [
    ({'market_type': 'secondary'}, ['9100000001', '9100000003']),
    ({'market_type': 'new'}, ['9100000002', '9100000004']),
    ({'rooms': 0}, ['9100000001']), ({'rooms': 'open_plan'}, ['9100000004']),
    ([('rooms', 2), ('rooms', 'open_plan')], ['9100000002', '9100000004']),
    ({'total_area_min': 50, 'total_area_max': 75}, ['9100000002', '9100000003']),
    ({'floor_min': 8, 'floor_max': 10}, ['9100000002', '9100000003']),
    ({'floors_total_min': 20, 'floors_total_max': 20}, ['9100000002']),
    ({'bathroom': 'combined'}, ['9100000001', '9100000003']),
    ([('bathroom', 'combined'), ('bathroom', 'separate')], ['9100000001', '9100000002', '9100000003']),
    ([('finish', 'без отделки'), ('finish', 'предчистовая')], ['9100000002']),
    ({'price_min': 2_500_000, 'price_max': 8_000_000}, ['9100000001', '9100000002']),
    ({'market_type': 'new', 'rooms': 2, 'total_area_min': 50, 'total_area_max': 50,
      'floor_min': 8, 'floor_max': 8, 'floors_total_min': 20, 'floors_total_max': 20,
      'bathroom': 'separate', 'finish': 'без отделки', 'price_min': 8_000_000, 'price_max': 8_000_000}, ['9100000002']),
])
def test_analytics_uses_same_filters_as_table_without_feature_duplicates(client, params, ids):
    response = client.get('/api/analytics', params=params)
    assert response.status_code == 200, response.text
    data = response.json()
    table = client.get('/api/listings', params=params).json()
    assert [point['avito_id'] for point in data['points']] == ids
    assert data['total'] == data['priced_count'] == table['total'] == len(ids)
    assert data['metrics']['price']['count'] == len(ids)
    assert data['metrics']['price']['mean'] == sum(item['price'] for item in table['items']) / len(ids)


def test_empty_and_singleton_selections(client):
    empty = client.get('/api/analytics?price_max=1').json()
    assert empty['total'] == empty['priced_count'] == 0 and empty['points'] == []
    assert all(metric == {'count': 0, 'mean': None, 'median': None, 'modes': [], 'mode_frequency': 0}
               for metric in empty['metrics'].values())
    one = client.get('/api/analytics?rooms=open_plan').json()
    assert one['metrics']['price'] == {'count': 1, 'mean': 15_000_000, 'median': 15_000_000,
                                      'modes': [15_000_000], 'mode_frequency': 1}


def test_missing_prices_only_excluded_from_price_statistics_and_charts(client, session_factory):
    with session_factory.begin() as session:
        listing_id = session.scalar(select(Listing.id).where(Listing.avito_id == '9100000001'))
        session.execute(delete(PriceObservation).where(PriceObservation.listing_id == listing_id))
    data = client.get('/api/analytics').json()
    assert data['total'] == 4 and data['priced_count'] == len(data['points']) == 3
    assert data['metrics']['total_area']['count'] == 4 and data['metrics']['total_area']['mean'] == 62.5
    assert data['metrics']['price']['count'] == data['metrics']['price_per_sqm']['count'] == 3
    assert data['metrics']['price']['mean'] == 10_666_666.67
    assert client.get('/api/analytics?price_min=0').json()['total'] == 3


def test_more_than_one_table_page_is_included_and_modes_use_current_prices(client, session_factory):
    at = datetime(2026, 10, 17, tzinfo=timezone.utc)
    with session_factory.begin() as session:
        base = session.scalar(select(Listing).where(Listing.avito_id == '9100000001'))
        for index in range(125):
            listing = Listing(avito_id=str(9200000000 + index), url=f'https://example.test/{index}',
                              title='Тест', market_type_code='secondary', address_id=base.address_id,
                              rooms=2, is_open_plan=False, total_area=Decimal('40'), first_seen_at=at, last_seen_at=at)
            session.add(listing)
            session.flush()
            # More observations must not give a listing greater weight.
            session.add_all([PriceObservation(listing_id=listing.id, amount=1_000_000, observed_at=at),
                             PriceObservation(listing_id=listing.id, amount=4_000_000, observed_at=at + timedelta(hours=1))])
    data = client.get('/api/analytics').json()
    assert data['total'] == data['priced_count'] == len(data['points']) == 129
    assert len({point['id'] for point in data['points']}) == 129
    assert data['metrics']['price'] == {'count': 129, 'mean': 4_143_410.85, 'median': 4_000_000,
                                       'modes': [4_000_000], 'mode_frequency': 125}
    assert data['metrics']['price_per_sqm'] == {'count': 129, 'mean': 101_007.75, 'median': 100_000,
                                               'modes': [100_000], 'mode_frequency': 126}
    assert data['metrics']['total_area'] == {'count': 129, 'mean': 40.7, 'median': 40,
                                            'modes': [40], 'mode_frequency': 125}


@pytest.mark.parametrize('params', [
    {'market_type': 'wrong'}, {'rooms': '-1'}, {'total_area_min': 'NaN'},
    {'price_min': 100, 'price_max': 1}, {'floor_min': 5, 'floor_max': 2},
    {'floors_total_min': 10, 'floors_total_max': 5}, {'bathroom': 'wrong'},
])
def test_invalid_analytics_filters_rejected(client, params):
    assert client.get('/api/analytics', params=params).status_code == 422
