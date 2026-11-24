"""A persistent pagination reset narrows a query; it never claims completeness."""
import hashlib
from dataclasses import replace
from urllib.parse import urlencode,urlsplit,urlunsplit

import pytest
from sqlalchemy import func, select

from app.catalog_navigation import catalog_url
from app.parser import CATALOG_URL
from app.crawl import next_task, start_browser_run, store_catalog
from app.models import CollectionRun, Crawl, CrawlItem, CrawlPage, CrawlRange, Listing, PriceObservation
from app.parser import parse_listing
from app.price_ranges import price_bounds, radius_bounds, range_url, decode_fields, encode_fields, RADIUS_ID
from app.repository import save_listing
from test_browser_api import browser_client, HEADERS, TOKEN
from test_local_browser import Clock, FakeAPI, ManualPage, job
from app import local_browser

OLD_URL = 'https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/dom_9990000111'
NEW_URL = 'https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/dom_9990000222'
DOCUMENT = f'''<main><div data-marker="catalog-serp"><div data-marker="item">
<a data-marker="item-title" href="{OLD_URL}">Дом</a></div></div>
<nav><span aria-current="page">1</span><a data-marker="pagination-button/nextPage" href="?p=2">Далее</a></nav></main>'''


@pytest.fixture
def reset_range(browser_client, session_factory, html, url):
    response = browser_client.post('/api/runs', json={'mode':'new','property_kind':'house'})
    rid = response.json()['id']
    claimed = browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':6})
    assert claimed.status_code == 200 and claimed.json()['catalog_recovery']
    with session_factory.begin() as session:
        run = session.get(CollectionRun,rid)
        crawl = session.get(Crawl,run.crawl_id)
        root = session.get(CrawlRange,(crawl.id,1))
        root.status = 'split'
        session.flush()
        part = CrawlRange(crawl_id=crawl.id,number=2,parent_number=1,minimum=5000001,maximum=10000000,status='active',next_page=19)
        session.add(part)
        session.flush()
        previous = catalog_url(range_url(crawl.catalog_url,part.minimum,part.maximum),18)
        session.add(CrawlPage(crawl_id=crawl.id,range_number=2,number=18,url=previous,has_next=True,
                              fingerprint=hashlib.sha256(OLD_URL.encode()).hexdigest()))
        session.flush()
        session.add(CrawlItem(crawl_id=crawl.id,avito_id='9990000111',url=OLD_URL,range_number=2,
                             page_number=18,position=0,status='saved'))
        original = parse_listing(html,url)
        row,_ = save_listing(session,replace(original,avito_id='9990000111',property_kind='house',url=OLD_URL))
        listing_id = row.id
        requested = catalog_url(range_url(crawl.catalog_url,part.minimum,part.maximum),19)
        actual = catalog_url(range_url(crawl.catalog_url,part.minimum,part.maximum),1)
        crawl_id = crawl.id
    return rid,crawl_id,listing_id,{'token':TOKEN,'url':requested,'page_number':19,'range_number':2,
                                  'actual_url':actual,'actual_page':1,'html':DOCUMENT}


def test_reset_page_19_splits_range_preserves_prices_and_deduplicates_next_catalog(browser_client,session_factory,reset_range):
    rid,cid,lid,body = reset_range
    path = f'/api/browser/runs/{rid}'
    response = browser_client.post(path+'/catalog-unreachable',headers=HEADERS,json=body)
    assert response.status_code == 200, response.text
    assert response.json()['action'] == 'split'
    # Repeated delivery cannot split a parent twice.
    assert browser_client.post(path+'/catalog-unreachable',headers=HEADERS,json=body).status_code == 409
    with session_factory() as session:
        parts = session.scalars(select(CrawlRange).where(CrawlRange.crawl_id==cid).order_by(CrawlRange.number)).all()
        assert parts[1].status == 'split'
        assert [(part.minimum,part.maximum) for part in parts[2:]] == [(5000001,7500000),(7500001,10000000)]
        assert session.get(Listing,lid) and session.scalar(select(func.count(PriceObservation.id))) == 1
        assert session.get(CollectionRun,rid).errors == 0
    task = browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()
    assert task['kind'] == 'catalog' and task['page_number'] == 1 and task['range_number'] == 3
    assert price_bounds(task['url']) == (5000001,7500000) and radius_bounds(task['url']) == 35
    document = DOCUMENT.replace('</div></div>',f'</div><div data-marker="item"><a data-marker="item-title" href="{NEW_URL}">Дом</a></div></div>')
    response = browser_client.post(path+'/catalog',headers=HEADERS,json={'token':TOKEN,'url':task['url'],
        'page_number':1,'range_number':3,'html':document})
    assert response.status_code == 200, response.text
    assert browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json() == {'kind':'listing','url':NEW_URL}
    with session_factory() as session:
        assert session.get(CrawlItem,(cid,'9990000111')).status == 'saved'


@pytest.mark.parametrize('case', ['captcha','wrong_price','wrong_radius','wrong_city','wrong_category','wrong_host','wrong_page','stale_task','pending','sample'])
def test_unconfirmed_reset_cannot_split_or_complete_range(browser_client,session_factory,reset_range,case):
    rid,cid,_,body = reset_range
    if case == 'captcha':body['html'] = '<h1>Подтвердите, что вы не робот</h1>'
    if case == 'wrong_price':body['actual_url'] = range_url(body['actual_url'],1,5000000)
    if case == 'wrong_radius':
        fields = decode_fields(body['actual_url']); fields[RADIUS_ID]['to'] = 40
        body['actual_url'] = urlunsplit(urlsplit(body['actual_url'])._replace(query=urlencode({'f':encode_fields('house',list(fields.items()))})))
    if case == 'wrong_city':body['actual_url'] = body['actual_url'].replace('/irkutsk/','/moskva/')
    if case == 'wrong_category':body['actual_url'] = body['actual_url'].replace('/doma_dachi_kottedzhi/','/kvartiry/')
    if case == 'wrong_host':body['actual_url'] = body['actual_url'].replace('www.avito.ru','evil.test')
    if case == 'wrong_page':body['actual_page'] = 2
    if case == 'stale_task':body['page_number'] = 20
    if case == 'pending':
        with session_factory.begin() as session:session.get(CrawlItem,(cid,'9990000111')).status = 'pending'
    if case == 'sample':
        with session_factory.begin() as session:session.get(Crawl,cid).mode = 'sample'
    response = browser_client.post(f'/api/browser/runs/{rid}/catalog-unreachable',headers=HEADERS,json=body)
    assert response.status_code in {409,422}, response.text
    with session_factory() as session:
        assert session.get(CrawlRange,(cid,2)).status == 'active'
        assert session.scalar(select(func.count()).select_from(CrawlRange).where(CrawlRange.crawl_id==cid)) == 2
        assert session.get(Crawl,cid).status == 'active'


def test_one_price_reset_marks_range_limited_without_looping_or_deleting_records(browser_client,session_factory,reset_range):
    rid,cid,lid,body = reset_range
    with session_factory.begin() as session:
        part = session.get(CrawlRange,(cid,2));part.minimum=part.maximum=7000000
    body['url'] = catalog_url(range_url(body['url'],7000000,7000000),19)
    body['actual_url'] = catalog_url(range_url(body['actual_url'],7000000,7000000),1)
    path = f'/api/browser/runs/{rid}'
    result = browser_client.post(path+'/catalog-unreachable',headers=HEADERS,json=body)
    assert result.status_code == 200 and result.json()['action'] == 'limited'
    task = browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()
    assert task['kind'] == 'done' and task['status'] == 'partial'
    assert 'Полнота сбора не подтверждена' in task['message']
    with session_factory() as session:
        assert session.get(Listing,lid) and session.scalar(select(func.count(PriceObservation.id))) == 1
        assert session.get(CrawlRange,(cid,2)).status == 'limited'


def test_reader_captures_reset_evidence_after_bounded_retry(monkeypatch):
    clock = Clock()
    class ResetPage(ManualPage):
        def goto(self,url,**options):super().goto(CATALOG_URL,**options)
        def content(self):return '<main>Каталог первой страницы</main>'
    page = ResetPage(clock,ready_at=1)
    monkeypatch.setattr(local_browser.time,'monotonic',clock.monotonic)
    with pytest.raises(local_browser.CatalogNavigationError) as captured:
        local_browser.BrowserJob(FakeAPI(),page,TOKEN,job()).read_page(CATALOG_URL+'?p=19','catalog')
    assert captured.value.actual_page == 1 and captured.value.actual_url == CATALOG_URL
    assert captured.value.html == '<main>Каталог первой страницы</main>' and len(page.actions) == 2


@pytest.mark.parametrize('previous_unreadable',[False,True])
def test_browser_sends_validated_reset_then_continues_to_narrower_query(monkeypatch,previous_unreadable):
    parent = {'kind':'catalog','url':CATALOG_URL+'?p=19','page_number':19,
              'range_number':7,'previous_url':CATALOG_URL+'?p=18'}
    narrow = {'kind':'catalog','url':CATALOG_URL+'?p=1','page_number':1,'range_number':8}
    tasks = iter([parent,narrow,{'kind':'done','status':'completed','message':'Готово'}] if previous_unreadable else
                 [parent,parent,narrow,{'kind':'done','status':'completed','message':'Готово'}])
    class API(FakeAPI):
        def call(self,path,body):
            super().call(path,body)
            if path.endswith('/next'):return next(tasks)
            if path.endswith('/catalog-unreachable'):return {'action':'split','message':'Диапазон разделён.'}
            return {'ok':True}
    api = API()
    runner = local_browser.BrowserJob(api,ManualPage(Clock()),TOKEN,
                {**job(),'catalog_recovery':True,'collection_mode':'full'})
    def read(url,kind):
        if url.endswith('19') or previous_unreadable and url.endswith('18'):
            raise local_browser.CatalogNavigationError('Сброс на первую страницу',actual_url=CATALOG_URL,
                                                       actual_page=1,html=DOCUMENT)
        return url,DOCUMENT
    monkeypatch.setattr(runner,'read_page',read)
    assert runner.run() == ('completed','Готово')
    deliveries = [body for path,body in api.calls if path.endswith('/catalog-unreachable')]
    assert len(deliveries) == 1 and deliveries[0]['page_number'] == 19 and deliveries[0]['range_number'] == 7
    assert not any(path.endswith('/listing') for path,_ in api.calls)


def test_sampling_never_splits_decile_on_navigation_error():
    runner = local_browser.BrowserJob(FakeAPI(),None,TOKEN,{**job(),'catalog_recovery':True,'collection_mode':'sample'})
    assert not runner.recover_catalog({'kind':'catalog','page_number':19},
        local_browser.CatalogNavigationError('Сброс',actual_url=CATALOG_URL,actual_page=1,html=DOCUMENT))
    assert not runner.api.calls


def test_recovery_survives_collector_restart(browser_client,session_factory,reset_range):
    rid,cid,_,body = reset_range
    path = f'/api/browser/runs/{rid}'
    assert browser_client.post(path+'/catalog-unreachable',headers=HEADERS,json=body).status_code == 200
    expected = browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()
    browser_client.post(path+'/finish',headers=HEADERS,json={'token':TOKEN,'status':'interrupted'})
    resumed = browser_client.post('/api/runs',json={'property_kind':'house','mode':'resume'}).json()['id']
    browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':6})
    assert browser_client.post(f'/api/browser/runs/{resumed}/next',headers=HEADERS,json={'token':TOKEN}).json() == expected
    with session_factory() as session:
        assert session.get(CollectionRun,resumed).crawl_id == cid
        assert session.get(CrawlItem,(cid,'9990000111')).status == 'saved'


def test_apartment_reset_splits_unfiltered_root_without_losing_expensive_tail(session):
    from app.crawl import recover_catalog_range
    run = start_browser_run(session,fresh=True)
    crawl = session.get(Crawl,run.crawl_id)
    part = session.get(CrawlRange,(crawl.id,1))
    part.next_page = 19
    result = recover_catalog_range(session,run,catalog_url(crawl.catalog_url,19),19,1,catalog_url(crawl.catalog_url,1))
    assert result['action'] == 'split'
    children = session.scalars(select(CrawlRange).where(CrawlRange.parent_number==1,CrawlRange.crawl_id==crawl.id)
                               .order_by(CrawlRange.minimum)).all()
    assert [(row.minimum,row.maximum) for row in children] == [(1,19999999),(20000000,None)]
    assert radius_bounds(next_task(session,run)['url']) is None
