from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import delete, func, select

from app.distributions import distribution, percentile
from app.duplicate_reviews import candidate_key
from app.models import DuplicateReview, Listing, ListingState, PriceObservation
from app.offer_classification import classify_offer, explicit_offer_features, stage
from app.parser import parse_listing
from app.repository import save_listing

AT = datetime(2026, 10, 20, tzinfo=timezone.utc)


def test_continuous_percentiles_are_interpolated_before_money_rounding():
    ordered = list(map(Decimal, ['1', '2', '8', '20']))
    assert percentile(ordered, '.1') == Decimal('1.3')
    assert percentile(ordered, '.25') == Decimal('1.75')
    assert percentile(ordered, '.75') == Decimal('11')
    assert percentile([], '.5') is None
    assert distribution([Decimal('.01'), Decimal('.02')])['p25'] == Decimal('.01')


@pytest.mark.parametrize('values', [[], [7], [7] * 50, [1, 2, 3, 10], list(range(100)) + [9_000_000_000],
                                      [Decimal('.01'), Decimal('.02'), Decimal('.03')], [100] * 200 + [1, 9999999]])
def test_histogram_counts_every_value_including_boundaries_and_extreme_tails(values):
    result = distribution(values)
    assert sum(bin['count'] for bin in result['bins']) == len(values)
    assert len(result['bins']) <= 34
    if values:
        assert result['popular_ranges']
        assert all(bin['count'] == max(item['count'] for item in result['bins']) for bin in result['popular_ranges'])
    else:
        assert result['p10'] is None and result['popular_ranges'] == []


def test_far_price_keeps_an_explicit_tail_without_flattening_normal_prices():
    result = distribution(list(range(1, 101)) + [9_000_000_000])
    assert result['bins'][-1]['kind'] == 'above'
    assert result['bins'][-1]['count'] == 1
    assert len([bin for bin in result['bins'] if bin['count']]) > 3
    assert result['popular_ranges'][0]['upper'] < 1000


def test_budget_is_inclusive_independent_of_price_filter_and_uses_only_known_prices(client, session_factory):
    result = client.get('/api/analytics?budget_min=8000000&budget_max=9000000').json()
    assert result['total'] == 4
    assert result['budget'] == {'minimum': 8000000, 'maximum': 9000000, 'configured': True,
                               'count': 2, 'priced_count': 4, 'share': 50}
    assert result['distributions']['price']['p10'] == 4_150_000
    assert result['distributions']['price']['p25'] == 6_625_000
    assert result['distributions']['price']['p75'] == 10_500_000
    assert result['distributions']['price']['p90'] == 13_200_000
    filtered = client.get('/api/analytics?budget_max=9000000&rooms=2').json()
    assert filtered['budget']['share'] == 100 and filtered['total'] == 1
    with session_factory.begin() as session:
        session.execute(delete(PriceObservation).where(PriceObservation.listing_id == select(Listing.id).where(Listing.avito_id == '9100000002').scalar_subquery()))
    missing = client.get('/api/analytics?budget_min=8000000&budget_max=9000000').json()
    assert missing['budget']['count'] == 1 and missing['budget']['priced_count'] == 3
    assert missing['budget']['share'] == 33.33
    assert missing['distributions']['total_area']['count'] == 4


@pytest.mark.parametrize('params', [{'budget_min': -1}, {'budget_max': 'NaN'}, {'budget_min': 2, 'budget_max': 1},
                                    {'compare_by': 'not-a-field'}, {'sale_scope': 'not-a-scope'}, {'construction_status': 'bad'}])
def test_invalid_insight_parameters_are_rejected(client, params):
    assert client.get('/api/analytics', params=params).status_code == 422


def test_offer_dimensions_are_independent_and_do_not_guess_from_silent_fields():
    assert explicit_offer_features({}) == {}
    offer = classify_offer('Доля: апартаменты-студия, 25 м²', 'apartment', 'new', {})
    assert offer == {'sale_scope': 'share', 'premise_kind': 'apartment', 'construction_status': 'unknown'}
    assert stage('готовность не указана') is None
    assert stage('не готов') == 'unfinished'
    assert stage('Строительство на заказ') == 'on_order'
    assert explicit_offer_features({'объект продажи':'Квартира целиком', 'тип помещения':'Апартаменты'}) == {
        'sale_scope':['whole'], 'premise_kind':['apartment']}
    assert explicit_offer_features({'доля':'1/2'})['sale_scope'] == ['share']
    assert classify_offer('Дом, 220 м²', 'house', 'unknown', {})['construction_status'] == 'unknown'


def test_html_captures_explicit_offer_data_and_district_but_ignores_description_negotiations(html, url):
    document = html.replace('</ul>', '<li>Объект продажи: целиком</li><li>Тип помещения: Апартаменты</li><li>Статус строительства: строится</li></ul>')
    document = document.replace('</main>', '<div id="item-view-address"><span>ул. Лермонтова, 100</span><span>р-н Свердловский</span></div><p>У собственника доли, продаётся вся квартира.</p></main>')
    result = parse_listing(document, url)
    assert result.features['sale_scope'] == ['whole']
    assert result.features['premise_kind'] == ['apartment']
    assert result.features['construction_status'] == ['building']
    assert result.features['district'] == ['Свердловский']
    plain = parse_listing(html.replace('</main>', '<p>У собственника доли, продаётся вся квартира.</p></main>'), url)
    assert classify_offer(plain.title, plain.property_kind, plain.market_type_code, plain.features)['sale_scope'] == 'whole'


def test_classification_and_district_filters_agree_in_table_and_analytics(client, session_factory, filter_data):
    data = replace(filter_data[0], title='Доля: 1-к. апартаменты, 25 м²', price=2_500_000,
                   features={**filter_data[0].features, 'district':['Свердловский']})
    with session_factory.begin() as session:
        save_listing(session, data, observed_at=AT)
        save_listing(session, replace(filter_data[1], features={**filter_data[1].features, 'construction_status':['building']}), observed_at=AT)
    for query in ['sale_scope=share', 'premise_kind=apartment', 'district=Свердловский', 'sale_scope=share&premise_kind=apartment']:
        table = client.get('/api/listings?' + query).json()
        stats = client.get('/api/analytics?' + query).json()
        assert [item['avito_id'] for item in table['items']] == [data.avito_id]
        assert [item['avito_id'] for item in stats['points']] == [data.avito_id]
        assert table['items'][0]['offer']['sale_scope'] == 'share'
        assert stats['quality']['offers']['sale_scope'][1]['count'] == 1
    assert client.get('/api/analytics?construction_status=building').json()['total'] == 1
    assert client.get('/api/analytics?district=__unknown__').json()['total'] == 3
    options = client.get('/api/listings/filter-options').json()
    assert {'value':'Свердловский','label':'Свердловский'} in options['districts']


@pytest.mark.parametrize('dimension', ['rooms', 'district', 'complex', 'area', 'finish', 'building_age', 'building_type', 'market_type', 'offer'])
def test_all_segment_dimensions_preserve_listing_counts_and_unpriced_members(client, dimension):
    result = client.get('/api/analytics', params={'compare_by':dimension}).json()
    groups = result['comparison']['rows']
    assert result['comparison']['dimension'] == dimension
    if result['comparison']['overlapping']:
        assert sum(row['count'] for row in groups) > result['total']
    else:
        assert sum(row['count'] for row in groups) == result['total']
    assert all(row['priced_count'] <= row['count'] for row in groups)
    assert all(row['p25_price'] <= row['median_price'] <= row['p75_price'] for row in groups if row['priced_count'])


def test_historical_offers_prices_segments_and_freshness_never_use_future_state(client, session_factory, filter_data):
    old = replace(filter_data[0], avito_id='9400000001', url='https://www.avito.ru/irkutsk/kvartiry/flat_9400000001',
                  title='Доля: апартаменты-студия, 25 м²', price=2_000_000)
    with session_factory.begin() as session:
        save_listing(session, old, observed_at=datetime(2026,9,15,tzinfo=timezone.utc))
        save_listing(session, replace(old,title='Квартира-студия, 25 м²',price=10_000_000),observed_at=AT)
    old_data = client.get('/api/analytics?month=2026-09&sale_scope=share&compare_by=offer&budget_max=3000000').json()
    assert old_data['total'] == 1
    assert old_data['distributions']['price']['p25'] == 2_000_000
    assert old_data['budget']['share'] == 100
    assert 'Доля' in old_data['comparison']['rows'][0]['label']
    assert old_data['comparison']['rows'][0]['freshness']['within_30_days'] == 1
    assert client.get('/api/analytics?sale_scope=share').json()['total'] == 0


def add_duplicate(session_factory, original, at=AT):
    data = replace(original, avito_id='9500000001', url='https://www.avito.ru/irkutsk/kvartiry/flat_9500000001')
    with session_factory.begin() as session:
        save_listing(session, data, observed_at=at)
    return data


def review_body(group, decision):
    a,b = group['members'][:2]
    return {'left_id':a['id'], 'right_id':b['id'], 'left_signature':a['signature'], 'right_signature':b['signature'], 'decision':decision}


def test_same_new_build_address_and_area_are_only_candidates_and_reviews_do_not_merge_data(client, session_factory, filter_data):
    add_duplicate(session_factory, filter_data[1])
    before = client.get('/api/analytics').json()
    groups = client.get('/api/analytics/duplicates').json()
    assert groups['total_groups'] == 1 and groups['candidate_count'] == 2
    assert before['total'] == 5 and before['quality']['duplicate_groups'] == 1
    group = groups['groups'][0]
    assert group['reviews'] == []
    with session_factory() as session:
        counts = [session.scalar(select(func.count(model.id))) for model in [Listing, ListingState, PriceObservation]]
    assert client.post('/api/analytics/duplicates/review', json=review_body(group,'different')).status_code == 200
    checked = client.get('/api/analytics/duplicates').json()['groups'][0]['reviews'][0]
    assert checked['decision'] == 'different' and not checked['stale']
    assert client.post('/api/analytics/duplicates/review', json=review_body(group,'same')).status_code == 200
    with session_factory() as session:
        assert [session.scalar(select(func.count(model.id))) for model in [Listing, ListingState, PriceObservation]] == counts
        assert session.scalar(select(func.count()).select_from(DuplicateReview)) == 2
    assert client.get('/api/analytics').json()['total'] == 5


def test_duplicate_reviews_survive_price_change_but_become_stale_after_attribute_changes(client, session_factory, filter_data):
    data = add_duplicate(session_factory, filter_data[1])
    group = client.get('/api/analytics/duplicates').json()['groups'][0]
    body = review_body(group,'same')
    assert client.post('/api/analytics/duplicates/review',json=body).status_code == 200
    with session_factory.begin() as session:
        save_listing(session,replace(data,price=data.price+1),observed_at=datetime(2026,10,21,tzinfo=timezone.utc))
    assert not client.get('/api/analytics/duplicates').json()['groups'][0]['reviews'][0]['stale']
    with session_factory.begin() as session:
        save_listing(session,replace(data,floor=7),observed_at=datetime(2026,10,22,tzinfo=timezone.utc))
    assert client.get('/api/analytics/duplicates').json()['groups'][0]['reviews'][0]['stale']
    assert client.post('/api/analytics/duplicates/review',json=body).status_code == 409


def test_duplicate_candidates_need_a_building_and_reviews_reject_external_origin(client, session_factory, filter_data):
    assert candidate_key({'address':'Иркутск'}) is None
    add_duplicate(session_factory,filter_data[1])
    group = client.get('/api/analytics/duplicates').json()['groups'][0]
    body = review_body(group,'different')
    assert client.post('/api/analytics/duplicates/review',json=body,headers={'Origin':'https://other.example'}).status_code == 403
    assert client.post('/api/analytics/duplicates/review',json={**body,'right_id':body['left_id']}).status_code == 422


def test_section_and_corpus_numbers_cannot_match_unrelated_street_addresses():
    row = dict(address='ул. Томсона, д. 6, секц. 12', map_key='секц|12', address_city_id=1,
               property_kind='apartment', total_area=Decimal('34.80'))
    other = {**row, 'address':'ул. Другая, д. 99, секц. 12'}
    assert candidate_key(row) != candidate_key(other)
    assert candidate_key(row) == candidate_key({**row, 'address':' УЛ. ТОМСОНА,  Д. 6, СЕКЦ. 12 '})


def test_monthly_duplicate_candidates_do_not_reveal_a_later_review(client, session_factory, filter_data):
    old = replace(filter_data[0], avito_id='9600000001', url='https://www.avito.ru/irkutsk/kvartiry/flat_9600000001')
    date = datetime(2026,9,15,tzinfo=timezone.utc)
    with session_factory.begin() as session:
        save_listing(session, old, observed_at=date)
    add_duplicate(session_factory, old, at=date)
    group = next(group for group in client.get('/api/analytics/duplicates').json()['groups'] if group['total_area'] == 25)
    assert client.post('/api/analytics/duplicates/review',json=review_body(group,'different')).status_code == 200
    historical = client.get('/api/analytics/duplicates?month=2026-09').json()
    assert historical['historical'] and historical['total_groups'] == 1
    assert historical['groups'][0]['reviews'] == []


def test_segment_freshness_and_missing_building_fields_are_explicit(client, session_factory):
    with session_factory.begin() as session:
        listing = session.scalar(select(Listing).where(Listing.avito_id == '9100000001'))
        listing.last_seen_at = datetime(2026,1,1,tzinfo=timezone.utc)
    rooms = client.get('/api/analytics?compare_by=rooms').json()['comparison']['rows']
    studio = next(row for row in rooms if row['key'] == '0')
    assert studio['freshness']['recent_share'] == 0
    years = client.get('/api/analytics?compare_by=building_age').json()['comparison']['rows']
    assert years == [{**years[0], 'key':'unknown', 'label':'Год постройки не указан', 'count':4}]


def test_duplicate_review_change_and_cancellation_keep_monthly_decisions(client, session_factory, filter_data, monkeypatch):
    from app import duplicate_reviews

    date = datetime(2026, 8, 1, tzinfo=timezone.utc)
    data = replace(filter_data[0], avito_id='9660000001', url='https://www.avito.ru/irkutsk/kvartiry/flat_9660000001')
    with session_factory.begin() as session:
        save_listing(session, data, observed_at=date)
    add_duplicate(session_factory, data, at=date)
    group = client.get('/api/analytics/duplicates?month=2026-08').json()['groups'][0]

    for month, decision in ((8, 'same'), (9, 'different'), (10, 'pending')):
        monkeypatch.setattr(duplicate_reviews, 'utcnow', lambda: datetime(2026, month, 15, tzinfo=timezone.utc))
        assert client.post('/api/analytics/duplicates/review', json=review_body(group, decision)).status_code == 200
    for month, expected in ((8, 'same'), (9, 'different'), (10, 'pending')):
        snapshot = client.get(f'/api/analytics/duplicates?month=2026-{month:02d}').json()['groups'][0]
        assert [item['decision'] for item in snapshot['reviews']] == [expected]


@pytest.mark.parametrize('features,district', [({}, 'Ленинский'), ({'district': ['Свердловский', 'Ленинский']}, 'Ленинский')])
def test_district_matches_in_table_map_and_analytics(client, session_factory, filter_data, features, district):
    data = replace(filter_data[0], avito_id='9660000003',
        url='https://www.avito.ru/irkutsk/kvartiry/flat_9660000003',
        address='Иркутск, Район Ленинский, ул. Ленина, 10', features=features)
    with session_factory.begin() as session:
        save_listing(session, data)
    params = {'district': district}
    listing = client.get('/api/listings', params=params).json()['items'][0]
    assert listing['district'] == district
    assert client.get('/api/map', params=params).json()['total'] == 1
    analytics = client.get('/api/analytics', params={**params, 'compare_by': 'district'}).json()
    assert analytics['total'] == 1 and analytics['comparison']['rows'][0]['label'] == district
