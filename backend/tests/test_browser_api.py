from datetime import timedelta

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from app.browser_jobs import expire_browser_runs
from app.config import settings
from app.db import get_session
from app.main import app
from app.models import CollectionRun, Listing, PriceObservation, utcnow

TOKEN = "a" * 32
OTHER = "b" * 32
HEADERS = {"X-Local-Collector": "1"}
CATALOG = "https://www.avito.ru/irkutsk/kvartiry/prodam-ASgBAgICAUSSA8YQ?p=1"


@pytest.fixture
def browser_client(session_factory, monkeypatch):
    monkeypatch.setattr(settings(), "collector_mode", "browser")
    def override():
        with session_factory() as session:
            yield session
    app.dependency_overrides[get_session] = override
    try:
        with TestClient(app) as client:
            yield client
    finally:
        app.dependency_overrides.clear()


def start(client, token=TOKEN, mode="resume"):
    response = client.post("/api/runs", json={"mode": mode})
    assert response.status_code == 202
    run_id = response.json()["id"]
    response = client.post("/api/browser/claim", json={"token": token, "protocol_version": 3}, headers=HEADERS)
    assert response.status_code == 200 and response.json()["run_id"] == run_id
    return run_id


def queue(client, run_id, url, token=TOKEN, page_number=1, has_next=False):
    html = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    if has_next is not None:
        html += '<button data-marker="pagination-button/nextPage"' + (' disabled' if not has_next else '') + '>Далее</button>'
    response = client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
                           json={"token": token, "url": CATALOG.replace("p=1", f"p={page_number}"),
                                 "html": html, "page_number": page_number})
    assert response.status_code == 200, response.text
    return response


def finish(client, run_id, token=TOKEN):
    path = f"/api/browser/runs/{run_id}"
    task = client.post(path + "/next", headers=HEADERS, json={"token": token}).json()
    assert task["kind"] == "done", task
    response = client.post(path + "/finish", headers=HEADERS,
                           json={"token": token, "status": task["status"], "message": task["message"]})
    assert response.status_code == 200
    return response.json()


def test_browser_collection_saves_prices_and_hides_lease(browser_client, session_factory, html, url):
    for document in (html, html.replace('content="7800000"', 'content="7500000"')):
        run_id = start(browser_client, mode="new")
        queue(browser_client, run_id, url)
        for attempt in range(2):  # The same delivery is acknowledged without another observation.
            response = browser_client.post(f"/api/browser/runs/{run_id}/listing", headers=HEADERS,
                                           json={"token": TOKEN, "url": url, "html": document})
            assert response.status_code == 200 and response.json()["saved"]
        public = browser_client.get("/api/runs/latest").json()
        assert public["saved"] == 1 and public["errors"] == 0
        assert "browser_token" not in public and TOKEN not in str(public)
        assert finish(browser_client, run_id)["status"] == "completed"
    assert public["price_changes"] == 1
    with session_factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 1
        assert session.scalars(select(PriceObservation.amount).order_by(PriceObservation.id)).all() == [7800000, 7500000]
        assert session.get(CollectionRun, run_id).browser_token is None


def test_saved_layout_is_stored_and_preserves_price_history(browser_client, hashed_listing):
    html, url = hashed_listing
    changed = html.replace("14\xa0750\xa0000", "14\xa0500\xa0000")
    assert changed != html
    for document in (html, html, changed):
        run_id = start(browser_client, mode="new")
        queue(browser_client, run_id, url)
        response = browser_client.post(f"/api/browser/runs/{run_id}/listing", headers=HEADERS,
                                       json={"token": TOKEN, "url": url, "html": document})
        assert response.status_code == 200 and response.json()["saved"]
        finish(browser_client, run_id)
    table = browser_client.get("/api/listings").json()
    assert table["total"] == 1
    row = table["items"][0]
    assert row["address"] == "ул. Тестовая, д. 14"
    assert row["complex_name"] == "ЖК «Тестовый квартал»"
    history = browser_client.get(f'/api/listings/{row["id"]}/prices').json()["items"]
    assert [item["amount"] for item in history] == [14750000, 14500000]
    run = browser_client.get("/api/runs/latest").json()
    assert run["saved"] == 1 and run["errors"] == 0 and run["price_changes"] == 1


def test_job_cannot_be_taken_or_finished_by_another_browser(browser_client):
    run_id = start(browser_client)
    own = browser_client.post("/api/browser/claim", headers=HEADERS, json={"token": TOKEN, "protocol_version": 3}).json()
    other = browser_client.post("/api/browser/claim", headers=HEADERS, json={"token": OTHER, "protocol_version": 3}).json()
    assert own["run_id"] == run_id and other["run_id"] is None
    assert browser_client.post(f"/api/browser/runs/{run_id}/heartbeat", headers=HEADERS,
                               json={"token": OTHER, "message": "Проверка"}).status_code == 409
    assert browser_client.post(f"/api/browser/runs/{run_id}/finish", headers=HEADERS,
                               json={"token": OTHER, "status": "completed"}).status_code == 409


def test_manual_check_heartbeat_keeps_lease_and_disconnect_releases_it(browser_client, session_factory):
    run_id = start(browser_client)
    with session_factory.begin() as session:
        session.get(CollectionRun, run_id).heartbeat_at = utcnow() - timedelta(seconds=110)
    response = browser_client.post(f"/api/browser/runs/{run_id}/heartbeat", headers=HEADERS,
                                   json={"token": TOKEN, "message": "Ожидается ручная CAPTCHA"})
    assert response.status_code == 200
    with session_factory.begin() as session:
        expire_browser_runs(session)
        assert session.get(CollectionRun, run_id).status == "running"
        session.get(CollectionRun, run_id).heartbeat_at = utcnow() - timedelta(seconds=121)
    with session_factory.begin() as session:
        expire_browser_runs(session)
    assert browser_client.get("/api/runs/latest").json()["status"] == "interrupted"
    assert browser_client.post(f"/api/browser/runs/{run_id}/heartbeat", headers=HEADERS,
                               json={"token": TOKEN, "protocol_version": 3}).status_code == 409
    assert start(browser_client, OTHER) != run_id


def test_parse_failure_is_visible_and_marks_run_partial(browser_client, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    response = browser_client.post(f"/api/browser/runs/{run_id}/listing", headers=HEADERS,
                                   json={"token": TOKEN, "url": url, "html": "<h1>Нет параметров</h1>"})
    assert response.json()["saved"] is False
    result = browser_client.post(f"/api/browser/runs/{run_id}/finish", headers=HEADERS,
                                 json={"token": TOKEN, "status": "completed", "message": "Готово"})
    assert result.json()["status"] == "partial"
    assert browser_client.get("/api/runs/latest").json()["errors"] == 1


@pytest.mark.parametrize("url", ["https://evil.example/irkutsk/kvartiry/prodam", "https://www.avito.ru/angarsk/kvartiry/prodam", "https://www.avito.ru:bad/irkutsk/kvartiry/prodam"])
def test_invalid_catalog_source_is_rejected(browser_client, url):
    run_id = start(browser_client)
    assert browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
                               json={"token": TOKEN, "url": url, "html": "<main></main>"}).status_code == 422


def test_cross_origin_and_form_posts_cannot_control_browser_jobs(browser_client):
    assert browser_client.post("/api/browser/claim", json={"token": TOKEN, "protocol_version": 3}).status_code == 403
    assert browser_client.post("/api/browser/claim", headers={**HEADERS, "Origin": "https://evil.example"},
                               json={"token": TOKEN, "protocol_version": 3}).status_code == 403


@pytest.mark.parametrize("fixture_name,reload", [("html", False), ("november_listing", True)])
def test_local_helper_and_backend_protocol_work_together_without_network(browser_client, html, url, monkeypatch, request, fixture_name, reload):
    from app.local_browser import BrowserJob, execute_job
    if fixture_name == "november_listing":
        html, url = request.getfixturevalue(fixture_name)
    assert browser_client.post("/api/runs").status_code == 202
    response = browser_client.post("/api/browser/claim", headers=HEADERS, json={"token": TOKEN, "protocol_version": 3})
    job = response.json()
    job["request_delay_seconds"] = 0
    class API:
        def call(self, path, body):
            response = browser_client.post("/api" + path, headers=HEADERS, json=body)
            assert response.status_code == 200, response.text
            return response.json()
    clock = [0]
    from app import local_browser
    monkeypatch.setattr(local_browser.time, "monotonic", lambda: clock[0])
    class Page:
        url = "about:blank"
        reloaded = False
        opened = []
        def evaluate(self, script):
            pass
        def wait_for_timeout(self, ms):
            clock[0] += ms / 1000
        def is_closed(self):
            return False
        def goto(self, target, **kwargs):
            self.url = target
            self.opened.append(target)
        def locator(self, selector):
            return self
        def count(self):
            return 1
        def all_text_contents(self):
            return ["Иркутск, ул. Лермонтова, 100"]
        def content(self):
            if "/prodam" in self.url:
                return f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div><button data-marker="pagination-button/nextPage" disabled>Далее</button>'
            if reload and not self.reloaded:
                self.reloaded = True
                raise RuntimeError("Unable to retrieve content because the page is navigating and changing the content")
            return html
    page = Page()
    assert execute_job(BrowserJob(API(), page, TOKEN, job, browser_errors=(RuntimeError,))) is True
    status = browser_client.get("/api/runs/latest").json()
    assert status["status"] == "completed" and status["saved"] == 1
    expected_price = 5_250_000 if fixture_name == "november_listing" else 7_800_000
    assert browser_client.get("/api/listings").json()["items"][0]["price"] == expected_price
    assert page.opened.count(url) == 1


def work(client, run_id, token=TOKEN):
    response = client.post(f"/api/browser/runs/{run_id}/next", headers=HEADERS, json={"token": token})
    assert response.status_code == 200, response.text
    return response.json()


def save(client, run_id, url, html, token=TOKEN):
    response = client.post(f"/api/browser/runs/{run_id}/listing", headers=HEADERS,
                           json={"token": token, "url": url, "html": html})
    assert response.status_code == 200, response.text
    return response.json()


def interrupt(client, run_id, token=TOKEN):
    response = client.post(f"/api/browser/runs/{run_id}/finish", headers=HEADERS,
                           json={"token": token, "status": "interrupted", "message": "Остановлено"})
    assert response.status_code == 200


def test_resume_mid_page_with_new_browser_and_then_next_page(browser_client, html, url):
    second = url.replace("1234567890", "1234567891")
    third = url.replace("1234567890", "1234567892")
    run_id = start(browser_client)
    document = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                       for u in [url, second])
    document += '<button data-marker="pagination-button/nextPage">Далее</button>'
    assert browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": CATALOG, "html": document}).status_code == 200
    assert save(browser_client, run_id, url, html)["saved"]
    crawl_id = browser_client.get("/api/progress").json()["crawl_id"]
    interrupt(browser_client, run_id)
    resumed = start(browser_client, token=OTHER)
    assert browser_client.get("/api/progress").json()["crawl_id"] == crawl_id
    assert work(browser_client, resumed, OTHER) == {"kind": "listing", "url": second}
    save(browser_client, resumed, second, html, OTHER)
    task = work(browser_client, resumed, OTHER)
    assert task["kind"] == "catalog" and task["page_number"] == 2
    queue(browser_client, resumed, third, OTHER, page_number=2)
    assert work(browser_client, resumed, OTHER) == {"kind": "listing", "url": third}
    save(browser_client, resumed, third, html, OTHER)
    assert finish(browser_client, resumed, OTHER)["status"] == "completed"
    assert browser_client.get("/api/progress").json()["saved"] == 3
    assert browser_client.get("/api/listings").json()["total"] == 3


def test_missing_pagination_probes_next_page_instead_of_finishing(browser_client, html, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url, has_next=None)
    save(browser_client, run_id, url, html)
    task = work(browser_client, run_id)
    assert task["kind"] == "catalog" and task["page_number"] == 2
    assert browser_client.get("/api/progress").json()["status"] == "active"
    response = browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": task["url"], "page_number": 2,
              "html": '<div data-marker="search-results/empty"></div>'})
    assert response.status_code == 200
    assert finish(browser_client, run_id)["status"] == "completed"


def test_repeated_catalog_is_reported_without_losing_cursor(browser_client, html, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url, has_next=None)
    save(browser_client, run_id, url, html)
    task = work(browser_client, run_id)
    duplicate = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    response = browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": task["url"], "page_number": 2, "html": duplicate})
    assert response.status_code == 409 and "Полнота" in response.json()["detail"]
    assert browser_client.get("/api/progress").json()["next_page"] == 2
    assert browser_client.get("/api/progress").json()["saved"] == 1


def test_previous_page_recheck_confirms_end_and_collects_new_ids(browser_client, html, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url, has_next=None)
    save(browser_client, run_id, url, html)
    assert work(browser_client, run_id)["page_number"] == 2
    second = url.replace("1234567890", "1234567891")
    document = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                       for u in [url, second])
    document += '<button data-marker="pagination-button/nextPage" disabled>Далее</button>'
    response = browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": CATALOG, "page_number": 1, "html": document, "recheck": True})
    assert response.status_code == 200
    assert work(browser_client, run_id) == {"kind": "listing", "url": second}
    save(browser_client, run_id, second, html)
    assert finish(browser_client, run_id)["status"] == "completed"
    assert browser_client.get("/api/progress").json()["saved"] == 2
    assert browser_client.get("/api/listings").json()["total"] == 2


def test_rewritten_catalog_url_is_checked_against_rendered_page(browser_client, html, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url, has_next=True)
    save(browser_client, run_id, url, html)
    assert work(browser_client, run_id)["page_number"] == 2
    second = url.replace("1234567890", "1234567891")
    document = f'<div data-marker="item"><a data-marker="item-title" href="{second}">Квартира</a></div>'
    document += '<nav><a href="?p=1">1</a><span aria-current="page">2</span></nav>'
    response = browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": CATALOG, "page_number": 2, "html": document})
    assert response.status_code == 200, response.text
    save(browser_client, run_id, second, html)
    assert finish(browser_client, run_id)["status"] == "completed"


def test_recheck_cannot_replace_unfinished_page(browser_client, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url, has_next=None)
    document = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    response = browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": CATALOG, "page_number": 1, "html": document, "recheck": True})
    assert response.status_code == 409
    assert browser_client.get("/api/progress").json()["queued"] == 1


def test_legacy_listings_are_skipped_only_during_initial_resume(browser_client, session_factory, html, url):
    from app.parser import parse_listing
    from app.repository import save_listing
    with session_factory.begin() as session:
        save_listing(session, parse_listing(html, url))
    assert browser_client.get("/api/progress").json()["legacy_count"] == 1
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    assert work(browser_client, run_id)["kind"] == "done"  # No detail request for an old saved ID.
    assert finish(browser_client, run_id)["status"] == "completed"
    run_id = start(browser_client, mode="new")
    queue(browser_client, run_id, url)
    assert work(browser_client, run_id) == {"kind": "listing", "url": url}
    save(browser_client, run_id, url, html.replace('content="7800000"', 'content="7500000"'))
    finish(browser_client, run_id)
    row = browser_client.get("/api/listings").json()["items"][0]
    assert row["price_count"] == 2 and row["price"] == 7500000


def test_failed_listing_can_be_retried_without_redoing_successful_items(browser_client, html, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    assert not save(browser_client, run_id, url, "<h1>Нет полей</h1>")["saved"]
    assert finish(browser_client, run_id)["status"] == "partial"
    assert browser_client.get("/api/progress").json()["can_resume"]
    run_id = start(browser_client)
    assert work(browser_client, run_id) == {"kind": "listing", "url": url}
    save(browser_client, run_id, url, html)
    assert finish(browser_client, run_id)["status"] == "completed"
    assert browser_client.get("/api/progress").json()["errors"] == 0


def test_completion_cannot_hide_pending_items(browser_client, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    response = browser_client.post(f"/api/browser/runs/{run_id}/finish", headers=HEADERS,
        json={"token": TOKEN, "status": "completed", "message": "Готово"})
    assert response.json()["status"] == "partial"
    assert browser_client.get("/api/progress").json()["queued"] == 1


def test_old_helper_is_rejected_before_claiming_a_job(browser_client):
    assert browser_client.post("/api/runs").status_code == 202
    response = browser_client.post("/api/browser/claim", headers=HEADERS, json={"token": TOKEN})
    assert response.status_code == 409 and "обновлён" in response.json()["detail"]
    assert browser_client.get("/api/runs/latest").json()["status"] == "queued"


def test_listing_limit_preserves_queue_for_next_run(browser_client, monkeypatch, html, url):
    monkeypatch.setattr(settings(), "max_listings", 1)
    run_id = start(browser_client)
    second = url.replace("1234567890", "1234567891")
    document = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                       for u in [url, second])
    document += '<button data-marker="pagination-button/nextPage" disabled>Далее</button>'
    assert browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": CATALOG, "html": document}).status_code == 200
    save(browser_client, run_id, url, html)
    task = work(browser_client, run_id)
    assert task["kind"] == "done" and task["status"] == "partial"
    browser_client.post(f"/api/browser/runs/{run_id}/finish", headers=HEADERS,
        json={"token": TOKEN, "status": task["status"], "message": task["message"]})
    resumed = start(browser_client)
    assert work(browser_client, resumed) == {"kind": "listing", "url": second}


def test_removed_listing_does_not_block_remaining_queue(browser_client, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    response = browser_client.post(f"/api/browser/runs/{run_id}/unavailable", headers=HEADERS,
        json={"token": TOKEN, "url": url, "actual_url": url, "status": 404})
    assert response.status_code == 200
    assert finish(browser_client, run_id)["status"] == "completed"
    assert browser_client.get("/api/progress").json()["skipped"] == 1


def test_open_plan_listing_is_saved_in_queue_and_exposed_in_table(browser_client, html, url):
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    document = html.replace("Количество комнат: 2", "Количество комнат: свободная планировка")
    assert save(browser_client, run_id, url, document)["saved"]
    assert finish(browser_client, run_id)["status"] == "completed"
    row = browser_client.get("/api/listings").json()["items"][0]
    assert row["rooms"] is None and row["is_open_plan"] is True
    assert row["price"] == 7800000
    assert browser_client.get("/api/progress").json()["errors"] == 0


@pytest.mark.parametrize("fixture_name", ["review_listing", "closed_listing", "expired_listing", "rejected_listing", "deleted_listing", "blocked_listing", "auction_listing"])
def test_unavailable_listing_is_skipped_and_next_listing_can_be_saved(browser_client, html, request, fixture_name):
    review_html, review_url = request.getfixturevalue(fixture_name)
    prefix, avito_id = review_url.rsplit("_", 1)
    next_url = f"{prefix}_{int(avito_id) + 1}"
    run_id = start(browser_client)
    catalog = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                      for u in [review_url, next_url])
    catalog += '<button data-marker="pagination-button/nextPage" disabled>Далее</button>'
    assert browser_client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS,
        json={"token": TOKEN, "url": CATALOG, "html": catalog}).status_code == 200
    response = save(browser_client, run_id, review_url, review_html)
    assert response["saved"] is False and "Сбор продолжается" in response["message"]
    assert save(browser_client, run_id, review_url, review_html) == response
    assert work(browser_client, run_id) == {"kind": "listing", "url": next_url}
    assert save(browser_client, run_id, next_url, html)["saved"]
    assert finish(browser_client, run_id)["status"] == "completed"
    progress = browser_client.get("/api/progress").json()
    assert progress["skipped"] == 1 and progress["saved"] == 1 and progress["errors"] == 0
    assert browser_client.get("/api/listings").json()["total"] == 1


@pytest.mark.parametrize("fixture_name", ["review_listing", "closed_listing", "blocked_listing", "auction_listing"])
def test_unavailable_status_preserves_existing_price_history(browser_client, session_factory, html, request, fixture_name):
    from app.parser import parse_listing
    from app.repository import save_listing
    review_html, url = request.getfixturevalue(fixture_name)
    with session_factory.begin() as session:
        row, _ = save_listing(session, parse_listing(html, url))
        last_seen = row.last_seen_at
    run_id = start(browser_client, mode="new")
    queue(browser_client, run_id, url)
    for attempt in range(2):
        assert not save(browser_client, run_id, url, review_html)["saved"]
    assert finish(browser_client, run_id)["status"] == "completed"
    row = browser_client.get("/api/listings", params={"listing_status": "all"}).json()["items"][0]
    assert row["price"] == 7800000 and row["price_count"] == 1
    with session_factory() as session:
        if fixture_name in {'closed_listing', 'blocked_listing'}:
            assert row['is_archived'] and row['archived_at']
            assert session.scalar(select(Listing.last_seen_at)) > last_seen
            assert browser_client.get('/api/listings').json()['total'] == 0
        else:
            assert not row['is_archived']
            assert session.scalar(select(Listing.last_seen_at)) == last_seen
        assert session.scalar(select(func.count(PriceObservation.id))) == 1
    assert browser_client.get("/api/progress").json()["skipped"] == 1


def test_failed_save_rolls_back_both_queue_and_price(browser_client, session_factory, monkeypatch, html, url):
    from app import browser_api
    from app.models import CrawlItem
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    original = browser_api.save_listing
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("Сбой перед подтверждением очереди")
    monkeypatch.setattr(browser_api, "save_listing", crash)
    with pytest.raises(RuntimeError, match="Сбой"):
        save(browser_client, run_id, url, html)
    with session_factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 0
        assert session.scalar(select(func.count(PriceObservation.id))) == 0
        assert session.scalar(select(CrawlItem.status)) == "pending"
    monkeypatch.setattr(browser_api, "save_listing", original)
    assert save(browser_client, run_id, url, html)["saved"]


def deliver_catalog(client, run_id, task, urls=(), has_next=False):
    document = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
                       for url in urls) or '<div data-marker="search-results/empty"></div>'
    if has_next is not None:
        document += '<button data-marker="pagination-button/nextPage"' + (' disabled' if not has_next else '') + '>Далее</button>'
    return client.post(f"/api/browser/runs/{run_id}/catalog", headers=HEADERS, json={
        "token": TOKEN, "url": task["url"], "page_number": task["page_number"],
        "range_number": task["range_number"], "html": document})


@pytest.mark.parametrize('last_has_next', [False, True, None])
def test_capped_query_splits_and_resumes_ranges_without_reopening_saved_ids(
        browser_client, session_factory, monkeypatch, html, url, last_has_next):
    from app import crawl
    from app.models import CrawlRange
    from app.price_ranges import price_bounds
    monkeypatch.setattr(crawl, 'CATALOG_PAGE_LIMIT', 2)
    second, third, fourth = (url.replace('1234567890', str(n)) for n in [1234567891, 1234567892, 1234567893])
    run_id = start(browser_client)
    for listing_url, end in [(url, True), (second, last_has_next)]:
        task = work(browser_client, run_id)
        assert deliver_catalog(browser_client, run_id, task, [listing_url], end).status_code == 200
        save(browser_client, run_id, listing_url, html)
    task = work(browser_client, run_id)
    assert task['page_number'] == 1 and task['range_number'] == 2
    assert price_bounds(task['url']) == (1, 19_999_999)
    assert work(browser_client, run_id) == task  # Re-delivery does not split again.
    assert deliver_catalog(browser_client, run_id, task, [url, second, third]).status_code == 200
    assert work(browser_client, run_id) == {'kind': 'listing', 'url': third}
    crawl_id = browser_client.get('/api/progress').json()['crawl_id']
    interrupt(browser_client, run_id)
    resumed = start(browser_client)
    assert browser_client.get('/api/progress').json()['crawl_id'] == crawl_id
    assert work(browser_client, resumed) == {'kind': 'listing', 'url': third}
    save(browser_client, resumed, third, html)
    tail = work(browser_client, resumed)
    assert price_bounds(tail['url']) == (20_000_000, None) and tail['page_number'] == 1
    assert deliver_catalog(browser_client, resumed, tail, [fourth]).status_code == 200
    save(browser_client, resumed, fourth, html)
    assert finish(browser_client, resumed)['status'] == 'completed'
    progress = browser_client.get('/api/progress').json()
    assert progress['saved'] == 4 and progress['ranges_completed'] == 2
    assert progress['ranges_remaining'] == 0 and progress['ranges_limited'] == 0
    with session_factory() as session:
        assert session.scalar(select(func.count(CrawlRange.number))) == 3
        assert session.scalar(select(func.count(PriceObservation.id))) == 4
        assert session.scalar(select(func.count(Listing.id))) == 4


def test_capped_closed_range_is_split_again_and_ids_are_deduplicated(browser_client, monkeypatch, html, url):
    from app import crawl
    from app.price_ranges import price_bounds
    monkeypatch.setattr(crawl, 'CATALOG_PAGE_LIMIT', 1)
    run_id = start(browser_client)
    assert deliver_catalog(browser_client, run_id, work(browser_client, run_id), [url]).status_code == 200
    save(browser_client, run_id, url, html)
    task = work(browser_client, run_id)
    assert price_bounds(task['url']) == (1, 19_999_999)
    assert deliver_catalog(browser_client, run_id, task, [url]).status_code == 200
    task = work(browser_client, run_id)
    assert task['kind'] == 'catalog' and price_bounds(task['url']) == (1, 10_000_000)
    progress = browser_client.get('/api/progress').json()
    assert progress['saved'] == 1 and progress['ranges_remaining'] == 3
    assert progress['status'] == 'active'


def test_reset_price_filter_and_late_range_delivery_do_not_advance_queue(browser_client, monkeypatch, html, url):
    from app import crawl
    monkeypatch.setattr(crawl, 'CATALOG_PAGE_LIMIT', 1)
    run_id = start(browser_client)
    root = work(browser_client, run_id)
    assert deliver_catalog(browser_client, run_id, root, [url]).status_code == 200
    save(browser_client, run_id, url, html)
    child = work(browser_client, run_id)
    assert child['range_number'] != root['range_number']
    reset = {**child, 'url': CATALOG}
    response = deliver_catalog(browser_client, run_id, reset, [url])
    assert response.status_code == 409 and 'фильтром цены' in response.json()['detail']
    assert deliver_catalog(browser_client, run_id, root, [url]).status_code == 409
    assert work(browser_client, run_id) == child


def test_page_limit_applies_across_ranges_and_preserves_next_range(browser_client, monkeypatch, html, url):
    from app import crawl
    monkeypatch.setattr(crawl, 'CATALOG_PAGE_LIMIT', 1)
    monkeypatch.setattr(settings(), 'max_pages', 1)
    run_id = start(browser_client)
    assert deliver_catalog(browser_client, run_id, work(browser_client, run_id), [url]).status_code == 200
    save(browser_client, run_id, url, html)
    assert finish(browser_client, run_id)['status'] == 'partial'
    resumed = start(browser_client)
    task = work(browser_client, resumed)
    assert task['kind'] == 'catalog' and task['range_number'] == 2 and task['page_number'] == 1


def test_single_price_cap_never_reports_full_coverage(browser_client, session_factory, monkeypatch, html, url):
    from app import crawl
    from app.models import CrawlRange
    monkeypatch.setattr(crawl, 'CATALOG_PAGE_LIMIT', 1)
    run_id = start(browser_client)
    with session_factory.begin() as session:
        part = session.scalar(select(CrawlRange))
        part.minimum = part.maximum = 7_800_000
    assert deliver_catalog(browser_client, run_id, work(browser_client, run_id), [url]).status_code == 200
    save(browser_client, run_id, url, html)
    task = work(browser_client, run_id)
    assert task['kind'] == 'done' and task['status'] == 'partial' and 'не подтверждена' in task['message']
    assert finish(browser_client, run_id)['status'] == 'partial'
    progress = browser_client.get('/api/progress').json()
    assert progress['status'] == 'active' and progress['ranges_limited'] == 1


def test_cursor_already_beyond_page_cap_uses_saved_page_without_requesting_31(
        browser_client, session_factory, monkeypatch, html, url):
    from app import crawl
    from app.models import CrawlRange
    monkeypatch.setattr(crawl, 'CATALOG_PAGE_LIMIT', 1)
    run_id = start(browser_client)
    assert deliver_catalog(browser_client, run_id, work(browser_client, run_id), [url], True).status_code == 200
    save(browser_client, run_id, url, html)
    with session_factory.begin() as session:
        session.scalar(select(CrawlRange)).next_page = 2
    task = work(browser_client, run_id)
    assert task['range_number'] == 2 and task['page_number'] == 1


def test_previous_protocol_cannot_collect_price_range_jobs(browser_client):
    assert browser_client.post('/api/runs').status_code == 202
    response = browser_client.post('/api/browser/claim', headers=HEADERS,
                                   json={'token': TOKEN, 'protocol_version': 2})
    assert response.status_code == 409
    assert browser_client.get('/api/runs/latest').json()['status'] == 'queued'


def test_missing_price_is_retried_twice_then_next_listing_is_saved(browser_client, html, url, monkeypatch):
    from bs4 import BeautifulSoup
    from app import local_browser
    from test_local_browser import Clock, ManualPage

    next_url = url.rsplit('_', 1)[0] + '_1234567891'
    run_id = start(browser_client)
    path = f'/api/browser/runs/{run_id}'
    document = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                       for u in (url, next_url)) + '<button data-marker="pagination-button/nextPage" disabled>Далее</button>'
    assert browser_client.post(path + '/catalog', headers=HEADERS,
        json={'token': TOKEN, 'url': CATALOG, 'html': document}).status_code == 200
    clock = Clock()
    monkeypatch.setattr(local_browser.time, 'monotonic', clock.monotonic)

    class Page(ManualPage):
        def goto(self, target, **options):
            super().goto(target, **options)
            self.soup = BeautifulSoup(self.content(), 'html.parser')

        def content(self):
            return '<h1>Квартира без цены</h1>' if self.url == url else html

        def locator(self, selector):
            tags = self.soup.select(selector)
            from types import SimpleNamespace
            return SimpleNamespace(count=lambda: len(tags), all_text_contents=lambda: [t.get_text() for t in tags])

    failures = []
    class API:
        def call(self, route, body):
            response = browser_client.post('/api' + route, headers=HEADERS, json=body)
            assert response.status_code == 200, response.text
            if route.endswith('/listing-error'):
                failures.append(body['attempt'])
                if len(failures) == 1:
                    raise local_browser.ApiTemporaryError('Потерян ответ после фиксации первой ошибки.')
            return response.json()

    page = Page(clock)
    runner = local_browser.BrowserJob(API(), page, TOKEN,
        dict(run_id=run_id, request_delay_seconds=1, request_timeout_seconds=5, browser_wait_seconds=2))
    assert local_browser.execute_job(runner)
    assert page.actions == ['goto', 'goto', 'goto']
    assert failures == [1, 1, 2]
    progress = browser_client.get('/api/progress').json()
    assert progress['errors'] == progress['saved'] == 1
    latest = browser_client.get('/api/runs/latest').json()
    assert latest['status'] == 'partial'
    resumed = start(browser_client)
    assert work(browser_client, resumed) == {'kind': 'listing', 'url': url}


@pytest.mark.parametrize('actual', [
    'https://www.avito.ru/irkutsk/kvartiry/other_1234567891',
    'https://www.avito.ru/irkutsk/kvartiry/prodam',
    'https://www.avito.ru:444/irkutsk/kvartiry/other_1234567890',
])
def test_unavailable_requires_matching_final_listing_url(browser_client, session_factory, html, url, actual):
    from app.parser import parse_listing
    from app.repository import save_listing

    with session_factory.begin() as session:
        row, _ = save_listing(session, parse_listing(html, url))
        listing_id = row.id
    run_id = start(browser_client, mode='new')
    queue(browser_client, run_id, url)
    response = browser_client.post(f'/api/browser/runs/{run_id}/unavailable', headers=HEADERS,
        json={'token': TOKEN, 'url': url, 'actual_url': actual, 'status': 404})
    assert response.status_code == 422
    assert work(browser_client, run_id)['url'] == url
    with session_factory() as session:
        assert not session.get(Listing, listing_id).is_archived


def test_expected_database_error_rolls_back_only_the_listing(browser_client, session_factory, html, url, monkeypatch):
    from dataclasses import replace
    from decimal import Decimal
    from app import browser_api

    original = browser_api.parse_listing
    monkeypatch.setattr(browser_api, 'parse_listing',
        lambda *args, **kwargs: replace(original(*args, **kwargs), total_area=Decimal('0.001')))
    run_id = start(browser_client)
    queue(browser_client, run_id, url)
    assert not save(browser_client, run_id, url, html)['saved']
    with session_factory() as session:
        assert session.scalar(select(func.count(Listing.id))) == 0
    assert finish(browser_client, run_id)['status'] == 'partial'
    assert browser_client.get('/api/progress').json()['errors'] == 1


def test_status_can_resume_full_after_refresh(browser_client, session_factory, html, url):
    from app.cities import default_city
    from app.models import CollectionRun
    from app.parser import parse_listing
    from app.repository import save_listing

    with session_factory.begin() as session:
        city_id = default_city(session).id
        save_listing(session, parse_listing(html, url))
    full = start(browser_client, mode='new')
    queue(browser_client, full, url)
    interrupt(browser_client, full)
    refresh = browser_client.post('/api/runs', json={'mode': 'new', 'collection_mode': 'refresh'}).json()['id']
    browser_client.post('/api/browser/claim', headers=HEADERS, json={'token': TOKEN, 'protocol_version': 7})
    assert save(browser_client, refresh, url, html)['saved']
    finish(browser_client, refresh)
    status = browser_client.get(f'/api/collection-status?city_id={city_id}').json()
    assert [item['mode'] for item in status['resumable']] == ['full']
    cached = browser_client.get('/api/collection-status', params={'city_id': city_id, 'since': status['version']}).json()
    assert 'progress' not in cached and cached['data_version'] == status['data_version']
    resumed = start(browser_client)
    assert work(browser_client, resumed)['url'] == url
    with session_factory() as session:
        assert session.get(CollectionRun, resumed).crawl_id == session.get(CollectionRun, full).crawl_id
