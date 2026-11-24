"""Regression for 10,000,001–19,999,999 rubles: Avito uses _, never - for +."""
import base64
from urllib.parse import parse_qs,urlsplit

import pytest
from sqlalchemy import func,select

from app.models import CrawlRange,CrawlItem,PriceObservation
from test_catalog_recovery import reset_range,DOCUMENT,NEW_URL
from test_browser_api import browser_client,HEADERS,TOKEN

from app import local_browser
from app.local_browser import BrowserJob,execute_job
from app.price_ranges import canonical_filter_url,price_bounds,price_filter,radius_bounds,MAX_PRICE
from test_local_browser import Clock,FakeAPI,ManualPage,job

BASE = 'https://www.avito.ru/irkutsk/doma_dachi_kottedzhi/prodam-ASgBAgICAUSUA9AQ'
ENCODED = 'ASgBAgECAUSUA9AQAkXGmgwfeyJmcm9tIjoxMDAwMDAwMSwidG8iOjE5OTk5OTk5feb_DhV7ImZyb20iOm51bGwsInRvIjozNX0'
TARGET = BASE+'?f='+ENCODED+'&p=1&localPriority=0'
LEGACY = TARGET.replace('feb_Dh','feb-Dh')


def test_failed_eight_digit_range_matches_avito_alphabet_and_binary_fields():
    encoded = price_filter(10000001,19999999,property_kind='house',radius_km=35)
    assert encoded == ENCODED and '-' not in encoded
    # Decode independently using Avito's alphabet; do not reuse the app's decoder.
    raw = base64.b64decode(encoded.replace('_','+').replace('~','/')+'='*(-len(encoded)%4),validate=True)
    assert raw.endswith(b'\xc6\x9a\x0c\x1f{"from":10000001,"to":19999999}\xe6\xfe\x0e\x15{"from":null,"to":35}')
    assert price_bounds(TARGET) == (10000001,19999999) and radius_bounds(TARGET) == 35


@pytest.mark.parametrize('bounds',[(1,None),(1000000,2000000),(5000001,10000000),(10000001,19999999),(20000000,None),(MAX_PRICE-1,MAX_PRICE)])
@pytest.mark.parametrize('radius',[5,35,100])
def test_price_digit_boundaries_preserve_both_fields(bounds,radius):
    encoded = price_filter(*bounds,property_kind='house',radius_km=radius)
    assert '-' not in encoded and '+' not in encoded and '/' not in encoded
    url = BASE+'?f='+encoded
    assert price_bounds(url) == bounds and radius_bounds(url) == radius


def test_legacy_saved_urls_are_read_but_reopened_in_avito_format():
    assert price_bounds(LEGACY) == (10000001,19999999) and radius_bounds(LEGACY) == 35
    canonical = canonical_filter_url(LEGACY)
    assert parse_qs(urlsplit(canonical).query) == parse_qs(urlsplit(TARGET).query)
    assert canonical_filter_url(TARGET) == TARGET
    assert canonical_filter_url(BASE) == BASE


def test_old_queue_url_is_normalized_before_browser_navigation(monkeypatch):
    clock = Clock()
    class RejectBadAlphabet(ManualPage):
        def goto(self,url,**options):
            self.requested = url
            super().goto(BASE if '-' in parse_qs(urlsplit(url).query).get('f',[''])[0] else url,**options)
    page = RejectBadAlphabet(clock,ready_at=1)
    monkeypatch.setattr(local_browser.time,'monotonic',clock.monotonic)
    actual,_ = BrowserJob(FakeAPI(),page,'a'*32,{**job(),'property_kind':'house'}).read_page(LEGACY,'catalog')
    assert price_bounds(actual) == (10000001,19999999) and radius_bounds(actual) == 35
    assert '-' not in parse_qs(urlsplit(page.requested).query)['f'][0] and page.actions == ['goto']


def test_transient_filter_reset_is_retried_once_then_returns_filtered_catalog(monkeypatch):
    clock = Clock()
    class FirstReset(ManualPage):
        def goto(self,url,**options):super().goto(BASE if not self.actions else url,**options)
    page = FirstReset(clock,ready_at=1)
    monkeypatch.setattr(local_browser.time,'monotonic',clock.monotonic)
    actual,_ = BrowserJob(FakeAPI(),page,'a'*32,{**job(),'property_kind':'house'}).read_page(TARGET,'catalog')
    assert actual == TARGET and page.actions == ['goto','goto']
    assert price_bounds(actual) == (10000001,19999999) and radius_bounds(actual) == 35


def test_delayed_filters_settle_without_extra_navigation(monkeypatch):
    clock = Clock()
    class DelayedFilters(ManualPage):
        def goto(self,url,**options):
            self.requested = url
            super().goto(BASE,**options)
        def wait_for_timeout(self,milliseconds):
            super().wait_for_timeout(milliseconds)
            if self.clock.now >= 2:self.url = getattr(self,'requested',BASE)
    page = DelayedFilters(clock,ready_at=1)
    monkeypatch.setattr(local_browser.time,'monotonic',clock.monotonic)
    actual,_ = BrowserJob(FakeAPI(),page,'a'*32,{**job(),'property_kind':'house'}).read_page(TARGET,'catalog')
    assert actual == TARGET and page.actions == ['goto']


def test_persistent_filter_reset_is_not_sent_to_server_as_filtered_data(monkeypatch):
    clock,api = Clock(),FakeAPI()
    class AlwaysReset(ManualPage):
        def goto(self,url,**options):super().goto(BASE,**options)
    page = AlwaysReset(clock,ready_at=1)
    monkeypatch.setattr(local_browser.time,'monotonic',clock.monotonic)
    runner = BrowserJob(api,page,'a'*32,{**job(),'property_kind':'house'})
    monkeypatch.setattr(runner,'run',lambda:runner.read_page(TARGET,'catalog'))
    assert execute_job(runner) is True
    assert page.actions == ['goto','goto']
    assert api.calls[-1][1]['status'] == 'failed'
    message = api.calls[-1][1]['message']
    assert '10 000 001–19 999 999' in message and 'радиус 35 км' in message and 'без фильтра цены' in message
    assert not any(path.endswith(('/catalog','/listing','/catalog-unreachable')) for path,_ in api.calls)


# Exercise the same literal URL through the server and its durable queue.


def test_server_accepts_corrected_eight_digit_filter_and_keeps_saved_items(browser_client,session_factory,reset_range):
    rid,cid,_,_ = reset_range
    with session_factory.begin() as session:
        part = session.get(CrawlRange,(cid,2))
        part.minimum,part.maximum,part.next_page = 10000001,19999999,1
    path = f'/api/browser/runs/{rid}'
    task = browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json()
    assert parse_qs(urlsplit(task['url']).query)['f'] == [ENCODED]
    document = DOCUMENT.replace('</div></div>',f'</div><div data-marker="item"><a data-marker="item-title" href="{NEW_URL}">Дом</a></div></div>')
    response = browser_client.post(path+'/catalog',headers=HEADERS,json={'token':TOKEN,'url':task['url'],'html':document,
                                  'range_number':2,'page_number':1})
    assert response.status_code == 200,response.text
    assert browser_client.post(path+'/next',headers=HEADERS,json={'token':TOKEN}).json() == {'kind':'listing','url':NEW_URL}
    with session_factory() as session:
        assert session.get(CrawlItem,(cid,'9990000111')).status == 'saved'
        assert session.scalar(select(func.count(PriceObservation.id))) == 1
