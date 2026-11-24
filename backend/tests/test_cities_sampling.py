from dataclasses import replace
from math import isqrt

import pytest
from sqlalchemy import func, select, update

from app.cities import city_catalog, default_city
from app.crawl import next_task, progress, start_browser_run, store_catalog
from app.models import City, CollectionRun, Crawl, CrawlItem, CrawlPage, CrawlRange, SampleBoundary, SamplePlan, SampleProbe
from app.parser import OutsideCity, ParseError, parse_catalog, parse_catalog_count, parse_listing
from app.price_ranges import range_url
from app.repository import save_listing
from app.sampling import prepare_sample, sample_next_task, store_probe
from test_browser_api import browser_client, HEADERS, TOKEN


def test_city_catalog_complete_unique_and_collected_first(client):
    data = client.get('/api/cities').json()
    assert len(data['items']) == 1117
    assert len({(row['name'], row['region']) for row in data['items']}) == 1117
    assert data['items'][0]['name'] == 'Иркутск' and data['items'][0]['listing_count'] == 4
    assert data['items'][0]['has_data']
    names = {row['name'] for row in data['items']}
    assert {'Москва', 'Санкт-Петербург', 'Воскресенск', 'Алупка', 'Ялта'} <= names
    assert len([row for row in data['items'] if row['name'] == 'Советск']) > 1


def test_city_isolation_in_table_options_and_analytics(client, session_factory, html, url):
    with session_factory.begin() as session:
        moscow = session.scalar(select(City).where(City.name == 'Москва'))
        irkutsk = default_city(session)
        data = parse_listing(html.replace('Иркутск', 'Москва'), url.replace('/irkutsk/','/moskva/'), city_name='Москва', city_slug='moskva')
        save_listing(session, replace(data, avito_id='9400000001', complex_name='Только Москва', price=42_000_000), city_id=moscow.id)
        mid, iid = moscow.id, irkutsk.id
    table = client.get('/api/listings', params={'city_id':mid}).json()
    assert table['total'] == table['total_all'] == 1 and table['items'][0]['price'] == 42_000_000
    assert client.get('/api/listings', params={'city_id':iid}).json()['total_all'] == 4
    analytic = client.get('/api/analytics', params={'city_id':mid}).json()
    assert analytic['total'] == 1 and analytic['metrics']['price']['mean'] == 42_000_000
    options = client.get('/api/listings/filter-options', params={'city_id':mid}).json()
    assert [row['name'] for row in options['complexes']] == ['Только Москва']
    assert client.get('/api/listings/filter-options', params={'city_id':iid}).json()['complexes'] != options['complexes']


def test_city_catalog_can_be_corrected_but_not_to_another_host(client):
    city_id = client.get('/api/cities').json()['default_city_id']
    assert client.put(f'/api/cities/{city_id}/catalog',json={'catalog_url':'https://evil.test/irkutsk/kvartiry/prodam'}).status_code == 422
    assert client.put(f'/api/cities/{city_id}/catalog',json={'catalog_url':'https://www.avito.ru/irkutsk/doma/prodam'}).status_code == 422
    response=client.put(f'/api/cities/{city_id}/catalog',json={'catalog_url':'https://www.avito.ru/irkutsk/kvartiry/prodam-ASgBAgICAUSSA8YQ?p=2'})
    assert response.status_code == 200 and response.json()['catalog_url'].endswith('/irkutsk/kvartiry/prodam')


@pytest.mark.parametrize('document,expected', [
    ('<span data-marker="page-title/count">7\u00a0009</span>',7009),
    ('<script>{"mainCount":1234}</script>',1234),
    ('<div data-marker="search-results/empty"></div>',0),
])
def test_catalog_counter(document, expected):
    assert parse_catalog_count(document) == expected


def test_catalog_counter_must_not_guess_from_other_numbers():
    with pytest.raises(ParseError): parse_catalog_count('<h1>Квартиры 2026</h1><p>15 000 000 ₽</p>')


def test_parser_accepts_selected_city_and_excludes_other_city(html,url):
    moscow_url=url.replace('/irkutsk/','/moskva/')
    with pytest.raises(OutsideCity): parse_listing(html,moscow_url,city_name='Москва',city_slug='moskva')
    row=parse_listing(html.replace('Иркутск','Москва'),moscow_url,city_name='Москва',city_slug='moskva')
    assert row.url==moscow_url and 'Москва' in row.address
    document=f'<div data-marker="item"><a data-marker="item-title" href="{moscow_url}">Квартира</a></div>'
    assert parse_catalog(document,source_url='https://www.avito.ru/moskva/kvartiry/prodam')[0]==[moscow_url]


def planned_sample(session, population=1000, same_price=False, nonlinear=False):
    run=start_browser_run(session,fresh=True,collection_mode='sample')
    crawl=session.get(Crawl,run.crawl_id)
    probes=0
    while (task:=prepare_sample(session,crawl,run)) is not None:
        maximum=task['maximum']
        count=population if maximum is None else (population if maximum>=5_000_000 else 0) if same_price else min(population, isqrt(maximum//1000) if nonlinear else maximum//1000)
        store_probe(session,crawl,run,task['url'],maximum,count)
        session.flush()
        probes+=1
        assert probes<300
    return crawl,run


def test_sample_deciles_come_from_counts_not_equal_price_spans(session):
    crawl,run=planned_sample(session,nonlinear=True)
    boundaries=session.scalars(select(SampleBoundary.price).where(SampleBoundary.crawl_id==crawl.id).order_by(SampleBoundary.number)).all()
    assert boundaries==[n*n*10_000_000 for n in range(1,10)]
    bands=session.scalars(select(CrawlRange).where(CrawlRange.crawl_id==crawl.id).order_by(CrawlRange.number)).all()
    assert len(bands)==10 and bands[-1].maximum is None
    assert [(b.minimum,b.maximum) for b in bands][:2]==[(1,10_000_000),(10_000_000,40_000_000)]
    assert sample_next_task(session,crawl,run)['range_number']==1


def test_sample_resumes_probe_and_has_independent_city_and_full_cursors(session):
    run=start_browser_run(session,collection_mode='sample')
    crawl=session.get(Crawl,run.crawl_id)
    task=prepare_sample(session,crawl,run)
    store_probe(session,crawl,run,task['url'],None,2000)
    run.status='interrupted'; session.flush()
    resumed=start_browser_run(session,collection_mode='sample')
    assert resumed.crawl_id==crawl.id and session.get(SamplePlan,crawl.id).total_count==2000
    assert prepare_sample(session,crawl,resumed)['maximum']==20_000_000
    resumed.status='interrupted'; session.flush()
    other=session.scalar(select(City).where(City.name=='Москва'))
    mrun=start_browser_run(session,city_id=other.id,collection_mode='full')
    assert mrun.crawl_id!=crawl.id and session.get(Crawl,mrun.crawl_id).city_id==other.id
    assert session.get(Crawl,crawl.id).status=='active'


def test_sample_quotas_exactly_100_each_and_unused_candidates_do_not_consume_other_bands(session,url):
    crawl,run=planned_sample(session)
    for number in range(1,11):
        task=sample_next_task(session,crawl,run)
        assert task['range_number']==number
        urls=[url.rsplit('_',1)[0]+'_'+str(9500000000+number*1000+i) for i in range(110)]
        store_catalog(session,run,task['url'],1,urls,True,range_number=number)
        items=session.scalars(select(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.range_number==number).order_by(CrawlItem.position)).all()
        for i,item in enumerate(items[:100]):
            task=sample_next_task(session,crawl,run)
            assert task['kind']=='listing' and task['url']==item.url
            item.status='saved'; session.flush()
        task=sample_next_task(session,crawl,run)
        assert task['kind']==('done' if number==10 else 'catalog')
        assert session.scalar(select(func.count()).select_from(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.status=='saved'))==number*100
    assert task['status']=='completed'
    assert session.scalar(select(func.count()).select_from(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.status=='pending'))==0
    groups = progress(session)['sample']['groups']
    assert [(group['number'], group['saved'], group['target'], group['status'])
            for group in groups] == [(n, 100, 100, 'completed') for n in range(1, 11)]
    assert groups[0]['minimum'] == 1 and groups[-1]['maximum'] is None


def test_equal_prices_keep_ten_bands_and_reassign_unselected_candidates_without_duplicates(session,url):
    crawl,run=planned_sample(session,same_price=True)
    assert session.scalars(select(SampleBoundary.price).where(SampleBoundary.crawl_id==crawl.id)).all()==[5_000_000]*9
    task=sample_next_task(session,crawl,run)
    urls=[url.rsplit('_',1)[0]+'_'+str(9600000000+i) for i in range(110)]
    store_catalog(session,run,task['url'],1,urls,True,range_number=1)
    session.execute(update(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.position<100).values(status='saved'))
    task=sample_next_task(session,crawl,run)
    assert task['range_number']==2
    store_catalog(session,run,task['url'],1,urls,False,range_number=2)
    assert session.scalar(select(func.count()).select_from(CrawlItem).where(CrawlItem.crawl_id==crawl.id))==110
    assert session.scalar(select(func.count()).select_from(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.range_number==2,CrawlItem.status=='pending'))==10


def test_empty_or_small_city_finishes_partial_without_faking_1000(session):
    crawl,run=planned_sample(session,population=0)
    done=sample_next_task(session,crawl,run)
    assert done['kind']=='done' and done['status']=='partial'


def test_probe_api_requires_new_collector_and_valid_counter(browser_client):
    response=browser_client.post('/api/runs',json={'mode':'new','collection_mode':'sample'})
    rid=response.json()['id']
    old=browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':3})
    assert old.status_code==409
    claim=browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':4})
    assert claim.status_code==200 and claim.json()['city_slug']=='irkutsk'
    task=browser_client.post(f'/api/browser/runs/{rid}/next',headers=HEADERS,json={'token':TOKEN}).json()
    assert task['purpose']=='sample_probe'
    invalid=browser_client.post(f'/api/browser/runs/{rid}/probe',headers=HEADERS,json={'token':TOKEN,'url':task['url'],'html':'<h1>Квартиры</h1>'})
    assert invalid.status_code==409
    valid=browser_client.post(f'/api/browser/runs/{rid}/probe',headers=HEADERS,json={'token':TOKEN,'url':task['url'],'html':'<span data-marker="page-title/count">2000</span>'})
    assert valid.status_code==200


def test_city_name_starting_with_g_is_not_mistaken_for_address_prefix(html,url):
    row=parse_listing(html.replace('Иркутск','Гатчина'),url.replace('/irkutsk/','/gatchina/'),city_name='Гатчина',city_slug='gatchina')
    assert row.address.startswith('Гатчина,')


def test_collector_sends_count_probe_to_separate_endpoint():
    from app.local_browser import BrowserJob
    class API:
        def __init__(self): self.calls=[]; self.tasks=[{'kind':'catalog','purpose':'sample_probe','url':'https://www.avito.ru/moskva/kvartiry/prodam','maximum':None},{'kind':'done','status':'completed','message':'Готово'}]
        def call(self,path,body):
            self.calls.append((path,body))
            return self.tasks.pop(0) if path.endswith('/next') else {'ok':True}
    api=API(); runner=BrowserJob(api,None,TOKEN,{'run_id':1,'city_slug':'moskva'})
    runner.read_page=lambda url,kind:(url,'<span data-marker="page-title/count">1000</span>')
    assert runner.run()==('completed','Готово')
    assert any(path.endswith('/probe') and body['maximum'] is None for path,body in api.calls)
    assert not any(path.endswith('/listing') for path,body in api.calls)


def test_sample_resume_keeps_saved_quota_and_next_pending_item(session,url):
    crawl,run=planned_sample(session)
    task=sample_next_task(session,crawl,run)
    urls=[url.rsplit('_',1)[0]+'_'+str(9700000000+i) for i in range(110)]
    store_catalog(session,run,task['url'],1,urls,True,range_number=1)
    session.execute(update(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.position<45).values(status='saved'))
    run.status='interrupted'; session.flush()
    resumed=start_browser_run(session,collection_mode='sample')
    assert resumed.crawl_id==crawl.id
    assert next_task(session,resumed)=={'kind':'listing','url':urls[45]}
    assert progress(session)['sample']['groups'][0]['saved']==45


def test_sample_resumes_short_band_then_advances_without_filling_quota(
        browser_client, session_factory, other_city_catalog, url):
    city_id = next(row['id'] for row in browser_client.get('/api/cities').json()['items']
                   if row['name'] == 'Усолье-Сибирское')
    request = {'mode': 'new', 'collection_mode': 'sample', 'city_id': city_id}
    rid = browser_client.post('/api/runs', json=request).json()['id']
    with session_factory.begin() as session:
        run = session.get(CollectionRun, rid)
        crawl = session.get(Crawl, run.crawl_id)
        crawl_id = crawl.id
        plan = session.get(SamplePlan, crawl.id)
        plan.phase, plan.total_count = 'collect', 290
        for number in range(1, 11):
            session.add(CrawlRange(crawl_id=crawl.id, number=number, status='pending',
                minimum=1 if number == 1 else 1_800_000 + (number - 2) * 100_000,
                maximum=1_800_000 + (number - 1) * 100_000 if number < 10 else None))
        session.flush()
        first = next_task(session, run)
        urls = [url.replace('/irkutsk/', '/usole-sibirskoe/').rsplit('_', 1)[0] + '_' + str(9900000000 + i)
                for i in range(29)]
        store_catalog(session, run, first['url'], 1, urls, True)
        session.execute(update(CrawlItem).where(CrawlItem.crawl_id == crawl.id).values(status='saved'))
        second = next_task(session, run)
        assert second['page_number'] == 2
        run.status = 'failed'

    request['mode'] = 'resume'
    resumed_id = browser_client.post('/api/runs', json=request).json()['id']
    claim = browser_client.post('/api/browser/claim', headers=HEADERS,
                               json={'token': TOKEN, 'protocol_version': 4})
    assert claim.status_code == 200 and claim.json()['run_id'] == resumed_id
    path = f'/api/browser/runs/{resumed_id}'
    task = browser_client.post(path + '/next', headers=HEADERS, json={'token': TOKEN}).json()
    assert task == second
    for number in range(1, 11):
        assert task['kind'] == 'catalog' and task['range_number'] == number
        document = other_city_catalog if number == 1 else other_city_catalog.replace('aria-current="page" data-marker="pagination-button/page(2)">2',
            'aria-current="page" data-marker="pagination-button/page(1)">1')
        response = browser_client.post(path + '/catalog', headers=HEADERS, json={
            'token': TOKEN, 'url': task['url'], 'html': document,
            'page_number': task['page_number'], 'range_number': number})
        assert response.status_code == 200, response.text
        assert response.json() == {'urls': [], 'has_next': False}
        task = browser_client.post(path + '/next', headers=HEADERS, json={'token': TOKEN}).json()
    assert task['kind'] == 'done' and task['status'] == 'partial'
    assert '29/1000' in task['message']
    with session_factory() as session:
        assert session.get(CollectionRun, resumed_id).crawl_id == crawl_id
        assert session.get(CollectionRun, resumed_id).errors == 0
        assert session.scalar(select(func.count()).select_from(CrawlItem).where(
            CrawlItem.crawl_id == crawl_id, CrawlItem.status == 'saved')) == 29
        assert session.scalar(select(func.count()).select_from(CrawlItem).where(CrawlItem.crawl_id == crawl_id)) == 29
        assert all(row.status == 'completed' for row in session.scalars(select(CrawlRange).where(CrawlRange.crawl_id == crawl_id)))


def test_api_cannot_save_101st_listing_in_sample_band(browser_client,session_factory,html,url):
    response=browser_client.post('/api/runs',json={'mode':'new','collection_mode':'sample'})
    rid=response.json()['id']
    browser_client.post('/api/browser/claim',headers=HEADERS,json={'token':TOKEN,'protocol_version':4})
    with session_factory.begin() as session:
        run=session.get(CollectionRun,rid); crawl=session.get(Crawl,run.crawl_id)
        session.get(SamplePlan,crawl.id).phase='collect'
        session.add(CrawlRange(crawl_id=crawl.id,number=1,minimum=1,maximum=10_000_000))
        session.flush()
        urls=[url.rsplit('_',1)[0]+'_'+str(9800000000+i) for i in range(100)]+[url]
        store_catalog(session,run,range_url(crawl.catalog_url,1,10_000_000),1,urls,False)
        session.execute(update(CrawlItem).where(CrawlItem.crawl_id==crawl.id,CrawlItem.position<100).values(status='saved'))
    response=browser_client.post(f'/api/browser/runs/{rid}/listing',headers=HEADERS,json={'token':TOKEN,'url':url,'html':html})
    assert response.status_code==200 and response.json()['saved'] is False
    assert '100' in response.json()['message']


@pytest.mark.parametrize('text',['7 тыс.','Более 1000','1,5 тыс.'])
def test_abbreviated_catalog_counter_is_not_treated_as_exact(text):
    with pytest.raises(ParseError):parse_catalog_count(f'<span data-marker="page-title/count">{text}</span>')
