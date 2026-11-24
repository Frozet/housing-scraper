"""Offline regressions for changing catalog counters and the interrupted user plan."""
import pytest
from sqlalchemy import select

from app.crawl import next_task, start_browser_run
from app.models import Crawl, CrawlRange, CollectionRun, SampleBoundary, SamplePlan, SampleProbe
from app.sampling import MAX_PROBE_ATTEMPTS, PRICE_STEP, prepare_sample, store_probe
from test_browser_api import browser_client, HEADERS, TOKEN


def begin_sample(session, population=1000):
    run = start_browser_run(session, fresh=True, collection_mode='sample')
    crawl = session.get(Crawl, run.crawl_id)
    task = prepare_sample(session, crawl, run)
    assert store_probe(session, crawl, run, task['url'], None, population)
    return crawl, run


@pytest.mark.parametrize('count,expected', [(499, 500), (991, 990), (750, 750)])
def test_small_changes_use_nearest_consistent_count(session, count, expected):
    crawl, run = begin_sample(session)
    task = prepare_sample(session, crawl, run)
    store_probe(session, crawl, run, task['url'], task['maximum'], 990)
    session.add(SampleProbe(crawl_id=crawl.id, maximum=5_000_000, count=500))
    session.flush()
    task = prepare_sample(session, crawl, run)
    assert task['maximum'] == 10_000_000
    assert store_probe(session, crawl, run, task['url'], task['maximum'], count)
    assert session.get(SampleProbe, (crawl.id, task['maximum'])).count == expected
    assert session.get(SamplePlan, crawl.id).probe_retries == 0


def test_small_increase_above_original_city_total_is_tolerated(session):
    crawl, run = begin_sample(session)
    task = prepare_sample(session, crawl, run)
    assert store_probe(session, crawl, run, task['url'], task['maximum'], 1002)
    assert session.get(SampleProbe, (crawl.id, task['maximum'])).count == 1000


def test_large_change_rechecks_same_price_then_recovers_without_storing_suspect_count(session):
    crawl, run = begin_sample(session)
    task = prepare_sample(session, crawl, run)
    assert not store_probe(session, crawl, run, task['url'], task['maximum'], 1400)
    assert session.get(SampleProbe, (crawl.id, task['maximum'])) is None
    assert session.get(SamplePlan, crawl.id).probe_retries == 1
    assert prepare_sample(session, crawl, run) == task
    assert store_probe(session, crawl, run, task['url'], task['maximum'], 990)
    assert session.get(SamplePlan, crawl.id).probe_retries == 0
    assert prepare_sample(session, crawl, run)['maximum'] == 10_000_000


def test_persistent_large_change_is_bounded_and_does_not_make_up_deciles(session):
    crawl, run = begin_sample(session)
    task = prepare_sample(session, crawl, run)
    for _ in range(MAX_PROBE_ATTEMPTS - 1):
        assert not store_probe(session, crawl, run, task['url'], task['maximum'], 1400)
    with pytest.raises(ValueError, match='Продолжить сбор'):
        store_probe(session, crawl, run, task['url'], task['maximum'], 1400)
    assert session.get(SamplePlan, crawl.id).probe_retries == 2
    assert session.scalar(select(CrawlRange).where(CrawlRange.crawl_id == crawl.id)) is None
    assert session.get(SampleProbe, (crawl.id, task['maximum'])) is None
    # An interrupted collector retains the failed task and its retry count.
    run.status = 'failed'
    session.flush()
    resumed = start_browser_run(session, collection_mode='sample')
    assert resumed.crawl_id == crawl.id
    assert prepare_sample(session, crawl, resumed) == task
    assert store_probe(session, crawl, resumed, task['url'], task['maximum'], 990)
    assert session.get(SamplePlan, crawl.id).probe_retries == 0


def test_changed_price_filter_is_not_accepted_as_counter_noise(session):
    crawl, run = begin_sample(session)
    task = prepare_sample(session, crawl, run)
    with pytest.raises(ValueError, match='изменил фильтр'):
        store_probe(session, crawl, run, crawl.catalog_url, task['maximum'], 1000)
    assert session.get(SamplePlan, crawl.id).probe_retries == 0


def test_resume_user_plan_stops_ruble_search_and_preserves_finished_boundaries(session):
    # Saved state of the user's failed Krasnoyarsk calculation: a one-ruble
    # interval at 30%, 63 cached counters, no listing collection started yet.
    crawl, run = begin_sample(session, population=15886)
    plan = session.get(SamplePlan, crawl.id)
    plan.phase = 'boundaries'
    for maximum, count in [(5_100_000, 1624), (5_921_440, 3178), (6_749_998, 4764),
                            (6_750_000, 4790), (10_000_000, 10807), (20_000_000, 15615)]:
        session.add(SampleProbe(crawl_id=crawl.id, maximum=maximum, count=count))
    for number in range(1, 10):
        low, high, price = (5_100_000, 5_100_000, 5_100_000) if number == 1 else (
            (5_921_440, 5_921_440, 5_921_440) if number == 2 else (
             (6_749_999, 6_750_000, None) if number == 3 else (1, 20_000_000, None)))
        session.add(SampleBoundary(crawl_id=crawl.id, number=number, low=low, high=high, price=price))
    run.status = 'failed'
    session.flush()
    resumed = start_browser_run(session, collection_mode='sample')
    task = next_task(session, resumed)
    assert resumed.crawl_id == crawl.id
    assert task['purpose'] == 'sample_probe' and task['maximum'] != 6_749_999
    assert session.get(SampleBoundary, (crawl.id, 1)).price == 5_100_000
    assert session.get(SampleBoundary, (crawl.id, 2)).price == 5_921_440
    assert session.get(SampleBoundary, (crawl.id, 3)).price == 6_750_000


def test_noisy_counters_finish_with_approximate_deciles_and_fewer_requests(session):
    crawl, run = begin_sample(session, population=15886)
    probes = 0
    while (task := prepare_sample(session, crawl, run)) is not None:
        maximum = task['maximum']
        count = min(15886, maximum // 1000) + (probes % 3 - 1)
        store_probe(session, crawl, run, task['url'], maximum, max(0, count))
        probes += 1
        assert probes < 100
    prices = session.scalars(select(SampleBoundary.price).where(SampleBoundary.crawl_id == crawl.id)
                              .order_by(SampleBoundary.number)).all()
    assert prices == sorted(prices)
    for number, price in enumerate(prices, 1):
        target = (15886 * number + 9) // 10
        assert abs(price - target * 1000) <= 2 * PRICE_STEP
    bands = session.scalars(select(CrawlRange).where(CrawlRange.crawl_id == crawl.id).order_by(CrawlRange.number)).all()
    assert len(bands) == 10 and bands[0].minimum == 1 and bands[-1].maximum is None
    assert all(left.maximum == right.minimum for left, right in zip(bands, bands[1:]))


def test_api_rechecks_counter_without_ending_browser_job(browser_client, session_factory):
    run_id = browser_client.post('/api/runs', json={'mode': 'new', 'collection_mode': 'sample'}).json()['id']
    assert browser_client.post('/api/browser/claim', headers=HEADERS,
        json={'token': TOKEN, 'protocol_version': 4}).status_code == 200
    path = f'/api/browser/runs/{run_id}'
    task = browser_client.post(path + '/next', headers=HEADERS, json={'token': TOKEN}).json()
    def send_count(task, count):
        return browser_client.post(path + '/probe', headers=HEADERS, json={
            'token': TOKEN, 'url': task['url'], 'maximum': task['maximum'],
            'html': f'<span data-marker="page-title/count">{count}</span>'})
    assert send_count(task, 1000).status_code == 200
    task = browser_client.post(path + '/next', headers=HEADERS, json={'token': TOKEN}).json()
    assert send_count(task, 1400).json() == {'ok': True, 'retry': True}
    assert browser_client.post(path + '/next', headers=HEADERS, json={'token': TOKEN}).json() == task
    assert send_count(task, 990).json() == {'ok': True}
    with session_factory() as session:
        assert session.get(CollectionRun, run_id).status == 'running'


def test_sample_page_budget_counts_probes_and_resumes(session, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings(), 'max_pages', 1)
    crawl, run = begin_sample(session)
    assert next_task(session, run)['status'] == 'partial'
    run.status = 'partial'
    session.flush()
    resumed = start_browser_run(session, collection_mode='sample')
    assert resumed.crawl_id == crawl.id
    assert next_task(session, resumed)['purpose'] == 'sample_probe'


def test_sample_listing_budget_keeps_the_rest_of_the_band(session, monkeypatch):
    from app.config import settings
    from app.crawl import store_catalog
    from app.models import CrawlItem

    run = start_browser_run(session, fresh=True, collection_mode='sample')
    crawl = session.get(Crawl, run.crawl_id)
    session.get(SamplePlan, crawl.id).phase = 'collect'
    session.add(CrawlRange(crawl_id=crawl.id, number=1, minimum=1, maximum=20000000))
    session.flush()
    task = next_task(session, run)
    sources = ['https://www.avito.ru/irkutsk/kvartiry/flat_101', 'https://www.avito.ru/irkutsk/kvartiry/flat_102']
    store_catalog(session, run, task['url'], 1, sources, False)
    first = session.get(CrawlItem, (crawl.id, '101'))
    first.status, first.last_run_id = 'saved', run.id
    monkeypatch.setattr(settings(), 'max_listings', 1)
    assert next_task(session, run)['status'] == 'partial'
    assert session.get(CrawlItem, (crawl.id, '102')).status == 'pending'
