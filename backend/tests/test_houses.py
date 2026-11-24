from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import func, select

from app.cities import city_catalog, default_city
from app.crawl import next_task, progress, start_browser_run, store_catalog
from app.listing_history import archive_listing
from app.local_browser import allowed_page
from app.models import CollectionRun, Crawl, Listing, ListingState, PriceObservation
from app.parser import ListingSkipped, OutsideCity, ParseError, house_number, parse_catalog, parse_listing
from app.price_ranges import price_bounds, price_filter, radius_bounds, range_url
from app.property_types import house_radius
from app.repository import save_listing
from app.sampling import prepare_sample, store_probe
from test_browser_api import browser_client, HEADERS, TOKEN

HOUSE_URL = 'https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/dom_220_m_na_uchastke_11_sot._9990000103'
USER_FILTER = 'ASgBAgECAUSUA9AQAkXGmgwdeyJmcm9tIjoxMDAwMDAwLCJ0byI6MjAwMDAwMH3m~g4VeyJmcm9tIjpudWxsLCJ0byI6MzV9'
CENTER = (Decimal('52.2864036'), Decimal('104.2807466'))


@pytest.fixture
def house_html():
    return (Path(__file__).parent / 'fixtures/house.html').read_text()


@pytest.fixture
def house(house_html):
    return parse_listing(house_html, HOUSE_URL, property_kind='house', city_center=CENTER, radius_km=35)


def test_house_records_all_fields_without_inventing_apartment_values(house):
    assert house.property_kind == 'house' and house.rooms == 4
    assert house.total_area == 220 and house.land_area == 11 and house.floors_total == 2
    assert house.floor is None and house.market_type_code == 'unknown'
    assert house.construction_year == 2024 and house.price == 36500000
    assert house.source_distance_km == 13 and house.distance_to_center_km == Decimal('12.60')
    assert 'Новолисиха' in house.address
    assert house.features == {
        'object_type': ['Дом'], 'land_category': ['индивидуальное жилищное строительство (ИЖС)'],
        'wall_material': ['кирпич'], 'terrace': ['есть'], 'bathroom': ['в доме'], 'finish': ['требует ремонта'],
        'electricity': ['есть'], 'heating': ['электрическое', 'жидкотопливный котёл'], 'water_supply': ['скважина'],
        'gas': ['нет'], 'sewerage': ['септик'], 'internet_tv': ['Wi-Fi', 'телевидение'], 'parking': ['гараж'],
        'transport_access': ['асфальтированная дорога', 'остановка общественного транспорта'],
        'infrastructure': ['магазин', 'аптека', 'детский сад', 'школа'], 'sale_conditions': ['Возможна ипотека'],
    }
    assert 'readiness' not in house.features


def test_optional_house_attributes_and_absent_rooms_are_valid(house_html):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(house_html, 'html.parser')
    for row in soup.select('[data-marker="item-view/item-params"] li'):
        if row.get_text().startswith('Количество комнат'):
            row.decompose()
    soup.select_one('[data-marker="item-view/item-params"] ul').append(BeautifulSoup(
        '<li>Вид объекта: Коттедж</li><li>Готовность: недостроенный</li><li>Для отдыха: баня, бассейн</li>', 'html.parser'))
    row = parse_listing(str(soup), HOUSE_URL, property_kind='house', city_center=CENTER, radius_km=35)
    assert row.rooms is None and not row.is_open_plan
    assert row.features['object_type'] == ['Коттедж']
    assert row.features['readiness'] == ['недостроенный'] and row.features['recreation'] == ['баня', 'бассейн']


@pytest.mark.parametrize('value,unit,result', [('0,11 га','land','11'), ('1100 м²','land','11'),
    ('11 сот.','land','11'), ('0 км','distance','0'), ('500 м','distance','.5'),
    ('не указано','land',None), ('0 сот.','land',None), ('-2 га','land',None)])
def test_house_units(value, unit, result):
    assert house_number(value, unit) == (Decimal(result) if result is not None else None)


def test_house_outside_city_slug_is_accepted_only_inside_radius(house_html):
    url = HOUSE_URL.replace('/irkutsk/', '/novo-lisiha/')
    assert parse_listing(house_html, url, property_kind='house', city_center=CENTER, radius_km=35).avito_id == '9990000103'
    with pytest.raises(OutsideCity):
        parse_listing(house_html, url, property_kind='house', city_center=CENTER, radius_km=5)
    with pytest.raises(ListingSkipped, match='координат'):
        parse_listing(house_html, url, property_kind='house', radius_km=35)
    assert allowed_page(url, property_kind='house', nearby=True)
    assert not allowed_page(url, property_kind='house')
    assert not allowed_page(url) and not allowed_page(HOUSE_URL.replace('avito.ru','evil.test'), property_kind='house', nearby=True)


def test_house_catalog_accepts_nearby_houses_and_never_apartments(house_html):
    nearby = HOUSE_URL.replace('/irkutsk/', '/novo-lisiha/')
    document = f'<div data-marker="item"><a data-marker="item-title" href="{nearby}">Дом</a></div>'
    assert parse_catalog(document, source_url='https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/prodam')[0] == [nearby]
    with pytest.raises(ParseError):
        parse_listing(house_html, HOUSE_URL)


def test_house_price_radius_encoding_matches_actual_user_url():
    assert price_filter(1000000, 2000000, property_kind='house', radius_km=35) == USER_FILTER
    supplied = 'https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/prodam?f=' + USER_FILTER
    assert price_bounds(supplied) == (1000000, 2000000) and radius_bounds(supplied) == 35
    for filtered in (True, False):
        result = range_url(supplied, 2000000, None, filtered=filtered)
        assert radius_bounds(result) == 35
        assert price_bounds(result) == ((2000000, None) if filtered else None)
        assert '/doma_dachi_kottedzhi/prodam-' in result


def test_auto_radius_is_bounded_and_city_override_is_respected():
    city = SimpleNamespace(house_radius_km=None, population=587225)
    assert house_radius(city) == 35
    city.population = 100
    assert house_radius(city) == 15
    city.population = 15000000
    assert house_radius(city) == 70
    city.population = None
    assert house_radius(city) == 25
    city.house_radius_km = Decimal(42)
    assert house_radius(city) == 42


def test_house_prices_archive_and_monthly_attributes_are_separate_from_apartments(client, session_factory, house):
    old, new = datetime(2026, 8, 1, tzinfo=timezone.utc), datetime(2026, 9, 1, tzinfo=timezone.utc)
    with session_factory.begin() as session:
        listing, _ = save_listing(session, house, observed_at=old)
        lid = listing.id
        save_listing(session, replace(house, price=35000000, land_area=Decimal(12)), observed_at=new, merge_missing=True)
        assert archive_listing(session, house.avito_id, 'Объявление удалено пользователем.', datetime(2026, 9, 15, tzinfo=timezone.utc))
    assert client.get('/api/listings').json()['total'] == 4
    params = {'property_kind':'house','listing_status':'all'}
    data = client.get('/api/listings', params=params).json()
    assert data['total'] == data['total_all'] == 1 and data['items'][0]['is_archived']
    assert data['items'][0]['land_area'] == 12 and data['items'][0]['price'] == 35000000
    prices = client.get(f'/api/listings/{lid}/prices').json()['items']
    assert [item['amount'] for item in prices] == [36500000,35000000]
    historic = client.get('/api/analytics', params={'property_kind':'house','month':'2026-08','land_area_max':11}).json()
    assert historic['total'] == 1 and historic['metrics']['price']['mean'] == 36500000
    assert historic['metrics']['land_area']['mean'] == 11
    assert client.get('/api/analytics', params={'property_kind':'house','month':'2026-09'}).json()['total'] == 0
    options = client.get('/api/listings/filter-options',params=params).json()
    assert options['house_features']['object_type'] == ['Дом'] and options['rooms'] == [{'value':'4','label':'4'}]
    mapped = client.get('/api/map', params=params).json()
    assert mapped['total'] == mapped['located'] == 1 and mapped['city']['radius_km'] == 35
    assert client.get('/api/map', params={'city_id':mapped['city']['id']}).json()['total'] == 4
    for key, value in [('land_area_min',13),('house_bathroom','на улице'),('heating','газовое')]:
        assert client.get('/api/listings',params={**params,key:value}).json()['total'] == 0
    assert client.get('/api/listings',params={**params,'house_bathroom':'в доме','heating':'электрическое'}).json()['total'] == 1


def test_house_crawl_requires_updated_browser_and_has_own_resume(browser_client, session_factory, house_html, house):
    response = browser_client.post('/api/runs', json={'property_kind':'house','mode':'new'})
    assert response.status_code == 202, response.text
    rid = response.json()['id']
    assert browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':4}).status_code == 409
    claimed = browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':5}).json()
    assert claimed['property_kind'] == 'house'
    path = f'/api/browser/runs/{rid}'
    task = browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()
    assert radius_bounds(task['url']) == 35
    catalog = f'<div data-marker="catalog-serp"><div data-marker="item"><a data-marker="item-title" href="{HOUSE_URL}">Дом</a></div></div><button data-marker="pagination-button/nextPage" disabled>Далее</button>'
    result = browser_client.post(path+'/catalog',headers=HEADERS,json={'token':TOKEN,'url':task['url'],'html':catalog})
    assert result.status_code == 200, result.text
    assert browser_client.post(path+'/listing',headers=HEADERS,json={'token':TOKEN,'url':HOUSE_URL,'html':house_html}).json()['saved']
    browser_client.post(path+'/finish',headers=HEADERS,json={'token':TOKEN,'status':'interrupted'})
    with session_factory.begin() as session:
        apartment_run = start_browser_run(session)
        apartment_id = apartment_run.crawl_id
        apartment_run.status = 'interrupted'
        resumed = start_browser_run(session, property_kind='house')
        assert resumed.crawl_id != apartment_id and progress(session, property_kind='house')['saved'] == 1
        assert progress(session)['saved'] == 0
        assert session.get(Crawl,resumed.crawl_id).radius_km == 35


def test_house_sample_keeps_radius_in_every_price_probe(session):
    run = start_browser_run(session, fresh=True, property_kind='house', collection_mode='sample')
    crawl = session.get(Crawl, run.crawl_id)
    probes = 0
    while (task := prepare_sample(session,crawl,run)) is not None:
        probes += 1
        assert probes < 200
        assert radius_bounds(task['url']) == 35 and '/doma_dachi_kottedzhi/' in task['url']
        maximum = task['maximum']
        store_probe(session,crawl,run,task['url'],maximum,1000 if maximum is None else min(1000, maximum // 10000))
    assert radius_bounds(next_task(session,run)['url']) == 35


def test_house_radius_setting_changes_future_crawl_not_apartment_progress(browser_client, session_factory):
    cities = browser_client.get('/api/cities',params={'property_kind':'house'}).json()
    iid = cities['default_city_id']
    assert browser_client.put(f'/api/cities/{iid}/house-radius',json={'radius_km':40}).json()['radius_km'] == 40
    assert browser_client.put(f'/api/cities/{iid}/house-radius',json={'radius_km':1000}).status_code == 422
    with session_factory.begin() as session:
        city = default_city(session)
        assert radius_bounds(city_catalog(city, 'house')) == 40
        assert radius_bounds(city_catalog(city)) is None
        run = start_browser_run(session,property_kind='house')
        assert session.get(Crawl,run.crawl_id).radius_km == 40
    assert browser_client.put(f'/api/cities/{iid}/house-radius',json={'radius_km':50}).status_code == 409


def test_overlapping_city_searches_share_one_house_and_price_history(client, session_factory, house, house_html):
    from app.models import City, ListingCityScope
    august = datetime(2026,8,1,tzinfo=timezone.utc)
    september = datetime(2026,9,1,tzinfo=timezone.utc)
    with session_factory.begin() as session:
        first = default_city(session)
        nearby = session.scalar(select(City).where(City.name == 'Шелехов'))
        iid, sid = first.id, nearby.id
        row, _ = save_listing(session,house,observed_at=august,city_id=iid)
        second = parse_listing(house_html, HOUSE_URL, property_kind='house', city_name=nearby.name,
                               city_center=(nearby.latitude,nearby.longitude), radius_km=70)
        save_listing(session,replace(second,price=36000000,rooms=None),observed_at=september,city_id=sid,merge_missing=True)
        assert row.rooms == 4
        assert session.scalar(select(func.count(Listing.id)).where(Listing.property_kind == 'house')) == 1
        assert session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.listing_id == row.id)) == 2
        assert session.scalar(select(func.count(ListingCityScope.listing_id)).where(ListingCityScope.listing_id == row.id)) == 2
    for city_id, distance in [(iid,house.distance_to_center_km),(sid,second.distance_to_center_km)]:
        data = client.get('/api/listings', params={'city_id':city_id,'property_kind':'house'}).json()
        assert data['total'] == 1 and data['items'][0]['distance_to_center_km'] == float(distance)
        assert data['items'][0]['price'] == 36000000
    assert client.get('/api/analytics',params={'city_id':sid,'property_kind':'house','month':'2026-08'}).json()['total'] == 0
    assert client.get('/api/analytics',params={'city_id':sid,'property_kind':'house','month':'2026-09','distance_to_center_km_max':second.distance_to_center_km}).json()['total'] == 1


def test_radius_comparison_uses_unrounded_distance(house_html, monkeypatch):
    monkeypatch.setattr('app.parser.distance_km',lambda *_:35.001)
    with pytest.raises(OutsideCity):
        parse_listing(house_html,HOUSE_URL,property_kind='house',city_center=CENTER,radius_km=35)


def test_house_browser_stops_when_radius_filter_is_lost(monkeypatch):
    from app import local_browser
    from test_local_browser import Clock, FakeAPI, ManualPage, job
    clock = Clock()
    class ResetRadiusPage(ManualPage):
        def goto(self,url,**options):
            super().goto('https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/prodam',**options)
    page = ResetRadiusPage(clock,ready_at=1)
    monkeypatch.setattr(local_browser.time,'monotonic',clock.monotonic)
    runner = local_browser.BrowserJob(FakeAPI(),page,TOKEN,{**job(),'property_kind':'house'})
    target = 'https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/prodam?f='+USER_FILTER
    with pytest.raises(local_browser.ClientError,match='радиуса'):
        runner.read_page(target,'catalog')
    assert page.actions == ['goto','goto']


def test_offline_house_import_uses_city_radius_and_preserves_actual_attributes(session_factory,house_html,tmp_path,monkeypatch,capsys):
    from app import collect
    source = tmp_path / 'house.html'
    source.write_text(house_html)
    monkeypatch.setattr('sys.argv',['collect','--html',str(source),'--url',HOUSE_URL,'--property-kind','house'])
    monkeypatch.setattr(collect,'SessionLocal',session_factory)
    collect.main()
    assert 'изменение_цены' in capsys.readouterr().out
    with session_factory() as session:
        house = session.scalar(select(Listing).where(Listing.avito_id=='9990000103'))
        assert house.property_kind == 'house' and house.land_area == 11 and len(house.scopes) == 1


def test_house_refresh_archives_closed_house_without_touching_apartments(browser_client,session_factory,house,html,url):
    with session_factory.begin() as session:
        save_listing(session,house)
        apartment,_ = save_listing(session,parse_listing(html,url))
        aid = apartment.id
    run = browser_client.post('/api/runs',json={'mode':'new','property_kind':'house','collection_mode':'refresh'}).json()
    browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':5})
    path = f'/api/browser/runs/{run["id"]}'
    task = browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()
    assert task == {'kind':'listing','url':HOUSE_URL}
    response = browser_client.post(path+'/listing',headers=HEADERS,json={'token':TOKEN,'url':HOUSE_URL,
        'html':'<h1>Объявление не посмотреть</h1><p>Пользователь его удалил.</p>'})
    assert response.status_code == 200 and not response.json()['saved']
    assert browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()['kind'] == 'done'
    with session_factory() as session:
        assert not session.get(Listing,aid).is_archived
        house_row = session.scalar(select(Listing).where(Listing.avito_id == house.avito_id))
        assert house_row.is_archived and 'удалено' in house_row.archive_reason
        assert session.get(CollectionRun,run['id']).archived == 1
        assert session.scalar(select(func.count(PriceObservation.id)).where(PriceObservation.listing_id==house_row.id)) == 1


def test_bootstrap_does_not_skip_house_known_only_in_neighbor_city(session,house):
    from app.models import City, CrawlItem
    first = default_city(session)
    neighbor = session.scalar(select(City).where(City.name=='Шелехов'))
    save_listing(session,house,city_id=first.id)
    own = replace(house,avito_id='9990000991',url=house.url.replace(house.avito_id,'9990000991'))
    save_listing(session,own,city_id=neighbor.id)
    run = start_browser_run(session,city_id=neighbor.id,property_kind='house')
    crawl = session.get(Crawl,run.crawl_id)
    task = next_task(session,run)
    store_catalog(session,run,task['url'],1,[house.url,own.url],False)
    assert session.get(CrawlItem,(crawl.id,house.avito_id)).status == 'pending'
    assert session.get(CrawlItem,(crawl.id,own.avito_id)).status == 'saved'


def test_resume_house_second_page_uses_the_saved_radius(session):
    from app.models import CollectionRun

    city = default_city(session)
    original = start_browser_run(session, property_kind='house')
    first = next_task(session, original)
    store_catalog(session, original, first['url'], 1, [], True, range_number=first['range_number'])
    assert next_task(session, original)['page_number'] == 2
    original.status = 'interrupted'
    city.house_radius_km = 50
    session.flush()

    resumed = start_browser_run(session, property_kind='house')
    assert resumed.crawl_id == original.crawl_id
    next_page = next_task(session, resumed)
    assert next_page['page_number'] == 2 and radius_bounds(next_page['url']) == 35
    resumed.status = 'interrupted'
    session.flush()
    fresh = start_browser_run(session, fresh=True, property_kind='house')
    assert fresh.crawl_id != resumed.crawl_id and radius_bounds(next_task(session, fresh)['url']) == 50
