import pytest

from app import local_browser
from app.catalog_navigation import catalog_url
from app.parser import CATALOG_URL
from app.local_browser import BrowserJob, ClientError, ListingReadError, LocalAPI, ManualCheckTimeout, execute_job


class FakeAPI:
    def __init__(self):
        self.calls = []
    def call(self, path, body):
        self.calls.append((path, body))
        return {"ok": True}


class Clock:
    now = 0
    def monotonic(self):
        return self.now


class ManualPage:
    def __init__(self, clock, ready_at=10):
        self.clock, self.ready_at = clock, ready_at
        self.url = "about:blank"
        self.actions = []
    def is_closed(self):
        return False
    def goto(self, url, **options):
        self.url = url
        self.actions.append("goto")
    def locator(self, selector):
        return self
    def evaluate(self, script):
        assert script == "window.scrollTo(0, document.documentElement.scrollHeight)"
    def count(self):
        return int(self.clock.now >= self.ready_at)
    def all_text_contents(self):
        return ["Иркутск, ул. Лермонтова, 100"] if self.count() else []
    def wait_for_timeout(self, milliseconds):
        self.clock.now += milliseconds / 1000
    def content(self):
        return "<main>После ручной проверки</main>"


def job():
    return {"run_id": 1, "request_delay_seconds": 1, "request_timeout_seconds": 10,
            "browser_wait_seconds": 30, "max_pages": 0, "max_listings": 0}


def test_manual_verification_waits_and_keeps_backend_alive(monkeypatch):
    clock, api = Clock(), FakeAPI()
    page = ManualPage(clock)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(api, page, "a" * 32, job())
    url, html = runner.read_page(CATALOG_URL, "catalog")
    assert url == CATALOG_URL and "После ручной" in html
    assert page.actions == ["goto"]  # No clicks, CAPTCHA solving or repeated navigation.
    assert any("загрузка каталога" in body.get("message", "").lower() or "загрузки каталога" in body.get("message", "").lower()
               for path, body in api.calls)
    assert not any("вручную" in body.get("message", "") for path, body in api.calls)
    assert len(api.calls) >= 2


def test_catalog_loading_timeout_is_partial_without_automatic_retry(monkeypatch):
    clock, api = Clock(), FakeAPI()
    page = ManualPage(clock, ready_at=1000)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(api, page, "a" * 32, {**job(), "request_delay_seconds": 5})
    monkeypatch.setattr(runner, "run", lambda: runner.read_page(CATALOG_URL, "catalog"))
    assert execute_job(runner) is True
    assert api.calls[-1][1]["status"] == "partial"
    assert page.actions == ["goto"]
    assert clock.now == 35


def test_listing_waits_for_delayed_address_without_navigating_again(monkeypatch):
    clock, api = Clock(), FakeAPI()
    class DelayedAddress(ManualPage):
        def locator(self, selector):
            if selector == local_browser.ADDRESS_SELECTOR:
                class Address:
                    def all_text_contents(self):
                        return ["Иркутск, ул. Лермонтова, 100"] if clock.now >= 8 else []
                return Address()
            if "streetAddress" in selector:
                class Empty:
                    def count(self):
                        return 0
                return Empty()
            return self
    page = DelayedAddress(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    url = "https://www.avito.ru/irkutsk/kvartiry/kvartira_123"
    assert BrowserJob(api, page, "a" * 32, job()).read_page(url, "listing")[0] == url
    assert clock.now == 8 and page.actions == ["goto"]
    assert any("загрузки адреса" in body.get("message", "") or "загрузка адреса" in body.get("message", "")
               for path, body in api.calls)


def test_missing_address_reaches_parser_after_bounded_wait(monkeypatch):
    clock, api = Clock(), FakeAPI()
    class MissingAddress(ManualPage):
        def locator(self, selector):
            self.selector = selector
            return self
        def count(self):
            return 0 if "streetAddress" in self.selector else 1
        def all_text_contents(self):
            return []
    page = MissingAddress(clock)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    url = "https://www.avito.ru/irkutsk/kvartiry/kvartira_123"
    BrowserJob(api, page, "a" * 32, job()).read_page(url, "listing")
    assert clock.now == 8 and page.actions == ["goto"]


@pytest.mark.parametrize("fixture_name", ["hashed_listing", "november_listing"])
def test_saved_layout_address_is_ready_without_extra_wait(monkeypatch, request, fixture_name):
    from bs4 import BeautifulSoup
    html, url = request.getfixturevalue(fixture_name)
    soup = BeautifulSoup(html, "html.parser")
    clock = Clock()
    class SavedPage(ManualPage):
        def locator(self, selector):
            tags = soup.select(selector)
            class Locator:
                def count(self):
                    return len(tags)
                def all_text_contents(self):
                    return [tag.get_text() for tag in tags]
            return Locator()
        def content(self):
            return html
    page = SavedPage(clock)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(FakeAPI(), page, "a" * 32, {**job(), "request_delay_seconds": 5})
    assert runner.read_page(url, "listing") == (url, html)
    assert clock.now == 2.5


def test_keyboard_interrupt_finishes_job_and_preserves_previous_results(monkeypatch):
    runner = BrowserJob(FakeAPI(), ManualPage(Clock()), "a" * 32, job())
    def stop():
        raise KeyboardInterrupt
    monkeypatch.setattr(runner, "run", stop)
    assert execute_job(runner) is False
    assert runner.api.calls[-1][1]["status"] == "interrupted"


@pytest.mark.parametrize("phase,error", [
    ("navigation", "Page.goto: net::ERR_ABORTED at https://www.avito.ru/irkutsk/kvartiry/foo_123"),
    ("navigation", "Navigation to page interrupted by another navigation"),
    ("heading", "Locator.all_text_contents: Execution context was destroyed, most likely because of a navigation"),
    ("address", "Cannot find context with specified id"),
    ("content", "Unable to retrieve content because the page is navigating and changing the content"),
])
def test_reload_during_reading_waits_without_reopening_or_losing_listing(monkeypatch, phase, error):
    clock = Clock()
    class ReloadingPage(ManualPage):
        failed = False
        def fail_once(self, at):
            if phase == at and not self.failed:
                self.failed = True
                raise RuntimeError(error)
        def goto(self, url, **options):
            super().goto(url, **options)
            self.fail_once("navigation")
        def locator(self, selector):
            self.fail_once("heading" if selector == "h1" else "address" if selector == local_browser.ADDRESS_SELECTOR else "other")
            return super().locator(selector)
        def content(self):
            self.fail_once("content")
            return super().content()
    page = ReloadingPage(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    url = "https://www.avito.ru/irkutsk/kvartiry/foo_123"
    runner = BrowserJob(FakeAPI(), page, "a" * 32, job(), browser_errors=(RuntimeError,))
    assert runner.read_page(url, "listing") == (url, "<main>После ручной проверки</main>")
    assert page.failed and page.actions == ["goto"] and clock.now < 3


def test_changed_url_while_capturing_html_is_rechecked_and_rejects_another_listing(monkeypatch):
    clock = Clock()
    class ChangedPage(ManualPage):
        def content(self):
            self.url = "https://www.avito.ru/irkutsk/kvartiry/another_456"
            return super().content()
    page = ChangedPage(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(FakeAPI(), page, "a" * 32, job())
    with pytest.raises(ClientError, match="другое объявление"):
        runner.read_page("https://www.avito.ru/irkutsk/kvartiry/foo_123", "listing")
    assert page.actions == ["goto"]


def test_regular_loading_timeout_does_not_claim_there_is_a_captcha(monkeypatch):
    clock = Clock()
    page = ManualPage(clock, ready_at=1000)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(FakeAPI(), page, "a" * 32, job())
    with pytest.raises(ListingReadError, match="объявления №123") as error:
        runner.read_page("https://www.avito.ru/irkutsk/kvartiry/foo_123", "listing")
    assert "заголовок, цена" in str(error.value)
    assert "проверку" not in str(error.value) and "CAPTCHA" not in str(error.value)


def test_repeated_document_reloads_stop_at_original_deadline(monkeypatch):
    clock = Clock()
    class ReloadLoop(ManualPage):
        def content(self):
            raise RuntimeError("Execution context was destroyed")
    page = ReloadLoop(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(FakeAPI(), page, "a" * 32, job(), browser_errors=(RuntimeError,))
    with pytest.raises(ListingReadError, match="перезагружается"):
        runner.read_page("https://www.avito.ru/irkutsk/kvartiry/foo_123", "listing")
    assert clock.now == 31 and page.actions == ["goto"]


def test_non_navigation_browser_error_is_reported_with_details_without_retry(monkeypatch, caplog):
    # Alembic's test setup disables loggers that existed before its configuration.
    monkeypatch.setattr(local_browser.LOGGER, "disabled", False)
    clock = Clock()
    class BrokenPage(ManualPage):
        def content(self):
            raise RuntimeError("Page crashed: https://user:password@www.avito.ru/irkutsk/kvartiry/foo_123?secret=value#private")
    page = BrokenPage(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(FakeAPI(), page, "a" * 32, job(), browser_errors=(RuntimeError,))
    monkeypatch.setattr(runner, "run", lambda: runner.read_page("https://www.avito.ru/irkutsk/kvartiry/foo_123", "listing"))
    assert execute_job(runner) is True
    finished = runner.api.calls[-1][1]
    assert finished["status"] == "failed" and "Page crashed" in finished["message"]
    assert "Page crashed" in caplog.text and "foo_123" in caplog.text
    for secret in ("password", "secret=value", "private"):
        assert secret not in caplog.text and secret not in finished["message"]
    assert page.actions == ["goto"]


@pytest.mark.parametrize("url", ["https://evil.example/api", "http://192.0.2.1/api", "https://localhost/api", "http://localhost/private"])
def test_html_can_only_be_sent_to_local_application(url):
    with pytest.raises(ClientError):
        LocalAPI(url)


def test_pagination_keeps_catalog_filters():
    assert catalog_url(CATALOG_URL + "-FILTER?localPriority=0&p=1", 2).endswith("localPriority=0&p=2")


def test_removed_listing_does_not_wait_for_captcha(monkeypatch):
    clock = Clock()
    class RemovedPage(ManualPage):
        def goto(self, url, **options):
            super().goto(url, **options)
            from types import SimpleNamespace
            return SimpleNamespace(status=404, url=url)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(FakeAPI(), RemovedPage(clock), "a" * 32, job())
    with pytest.raises(local_browser.ListingUnavailable):
        runner.read_page("https://www.avito.ru/irkutsk/kvartiry/foo_123", "listing")
    assert clock.now == 1


@pytest.mark.parametrize("status", [200, 404, 410])
@pytest.mark.parametrize("fixture_name,keep_auction_title", [
    ("review_listing", True), ("closed_listing", True), ("expired_listing", True), ("rejected_listing", True),
    ("deleted_listing", True), ("blocked_listing", True),
    ("auction_listing", True), ("auction_listing", False),
])
def test_unavailable_page_is_forwarded_immediately_without_price_or_address(
        monkeypatch, request, fixture_name, status, keep_auction_title):
    from bs4 import BeautifulSoup
    html, url = request.getfixturevalue(fixture_name)
    if not keep_auction_title:
        html = html.replace("Аукцион: ", "")
    soup = BeautifulSoup(html, "html.parser")
    clock = Clock()
    class ReviewPage(ManualPage):
        def goto(self, target, **options):
            super().goto(target, **options)
            class Response:
                pass
            response = Response()
            response.status, response.url = status, target
            return response
        def locator(self, selector):
            tags = soup.select(selector)
            class Locator:
                def count(self):
                    return len(tags)
                def all_text_contents(self):
                    return [tag.get_text() for tag in tags]
            return Locator()
        def content(self):
            return html
    page = ReviewPage(clock)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    assert BrowserJob(FakeAPI(), page, "a" * 32, job()).read_page(url, "listing") == (url, html)
    assert clock.now == 1 and page.actions == ["goto"]


def test_browser_verification_is_not_treated_as_listing_moderation(monkeypatch):
    clock = Clock()
    class CaptchaPage(ManualPage):
        def locator(self, selector):
            class Locator:
                def count(self):
                    return 1 if selector == "h1" else 0
                def all_text_contents(self):
                    return ["Проверка браузера"] if selector == "h1" else ["Подтвердите, что вы не робот"]
            return Locator()
    page = CaptchaPage(clock)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    with pytest.raises(ManualCheckTimeout, match="Пройдите её вручную"):
        BrowserJob(FakeAPI(), page, "a" * 32, job()).read_page("https://www.avito.ru/irkutsk/kvartiry/foo_123", "listing")
    assert page.actions == ["goto"]


def test_catalog_accepts_rewritten_query_when_rendered_page_matches(monkeypatch):
    from app.catalog_navigation import CURRENT_PAGE_SELECTOR
    clock = Clock()
    class RewrittenPage(ManualPage):
        def goto(self, url, **options):
            super().goto(url.split('?')[0], **options)
        def locator(self, selector):
            if selector == CURRENT_PAGE_SELECTOR:
                class Active:
                    def all_text_contents(self):
                        return ["31"]
                return Active()
            return self
    page = RewrittenPage(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    target = CATALOG_URL + '?p=31'
    actual, _ = BrowserJob(FakeAPI(), page, "a" * 32, job()).read_page(target, "catalog")
    assert actual == CATALOG_URL and page.actions == ["goto"]


def test_catalog_redirect_retries_once_then_keeps_window_open(monkeypatch):
    clock = Clock()
    class RedirectPage(ManualPage):
        def goto(self, url, **options):
            super().goto(CATALOG_URL, **options)
    page, api = RedirectPage(clock, ready_at=1), FakeAPI()
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    runner = BrowserJob(api, page, "a" * 32, job())
    monkeypatch.setattr(runner, "run", lambda: runner.read_page(CATALOG_URL + '?p=31', "catalog"))
    assert execute_job(runner) is True
    assert page.actions == ["goto", "goto"]
    assert api.calls[-1][1]["status"] == "partial"
    assert "31" in api.calls[-1][1]["message"] and "1" in api.calls[-1][1]["message"]


def test_temporary_catalog_redirect_recovers_on_second_navigation(monkeypatch):
    clock = Clock()
    class TemporaryPage(ManualPage):
        def goto(self, url, **options):
            super().goto(url if self.actions else CATALOG_URL, **options)
    page = TemporaryPage(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, "monotonic", clock.monotonic)
    target = CATALOG_URL + '?p=31'
    assert BrowserJob(FakeAPI(), page, "a" * 32, job()).read_page(target, "catalog")[0] == target
    assert page.actions == ["goto", "goto"]


def test_catalog_boundary_is_rechecked_once_without_infinite_retry(monkeypatch):
    class BoundaryAPI(FakeAPI):
        def call(self, path, body):
            super().call(path, body)
            if path.endswith('/next'):
                return {"kind": "catalog", "url": CATALOG_URL + '?p=31',
                        "page_number": 31, "previous_url": CATALOG_URL + '?p=30'}
            return {"ok": True}
    api = BoundaryAPI()
    runner = BrowserJob(api, ManualPage(Clock()), "a" * 32, job())
    navigations = []
    def read(url, kind):
        navigations.append(url)
        if url.endswith('31'):
            raise local_browser.CatalogNavigationError("Открыта другая страница")
        return url, '<main>Последняя страница</main>'
    monkeypatch.setattr(runner, 'read_page', read)
    assert execute_job(runner) is True
    assert len(navigations) == 3
    rechecks = [body for path, body in api.calls if path.endswith('/catalog')]
    assert len(rechecks) == 1 and rechecks[0]['recheck'] is True and rechecks[0]['page_number'] == 30
    assert api.calls[-1][1]['status'] == 'partial'


def test_recoverable_client_error_leaves_browser_available(monkeypatch):
    runner = BrowserJob(FakeAPI(), ManualPage(Clock()), "a" * 32, job())
    def fail():
        raise ClientError("Прогресс сохранён")
    monkeypatch.setattr(runner, 'run', fail)
    assert execute_job(runner) is True
    assert runner.api.calls[-1][1]['status'] == 'failed'


def test_catalog_page_identity_normalizes_query_and_rejects_ambiguous_markup():
    from app.catalog_navigation import catalog_page, page_in_url
    base = CATALOG_URL
    assert page_in_url(base + '?p=031&p=31') == 31
    assert page_in_url(base + '?p=0') is None
    assert catalog_page(base + '?p=1', ['31']) == 31
    assert catalog_page(base + '?p=31', ['30', '31']) is None


def test_price_filter_reset_leaves_progress_and_browser_open(monkeypatch):
    from app.price_ranges import range_url
    clock = Clock()
    class ResetPricePage(ManualPage):
        def goto(self, url, **options):
            super().goto(CATALOG_URL, **options)
    page = ResetPricePage(clock, ready_at=1)
    monkeypatch.setattr(local_browser.time, 'monotonic', clock.monotonic)
    runner = BrowserJob(FakeAPI(), page, 'a' * 32, job())
    target = range_url(CATALOG_URL, 1_000_000, 2_000_000)
    monkeypatch.setattr(runner, 'run', lambda: runner.read_page(target, 'catalog'))
    assert execute_job(runner) is True
    assert runner.api.calls[-1][1]['status'] == 'failed'
    assert 'фильтр цены' in runner.api.calls[-1][1]['message']


def test_catalog_recheck_is_independent_for_each_price_range(monkeypatch):
    tasks = iter([
        {'kind': 'catalog', 'url': 'next', 'previous_url': 'previous', 'page_number': 3, 'range_number': 2},
        {'kind': 'catalog', 'url': 'next', 'previous_url': 'previous', 'page_number': 3, 'range_number': 3},
        {'kind': 'done', 'status': 'completed', 'message': 'Готово'},
    ])
    class API(FakeAPI):
        def call(self, path, body):
            super().call(path, body)
            return next(tasks) if path.endswith('/next') else {'ok': True}
    api = API()
    runner = BrowserJob(api, ManualPage(Clock()), 'a' * 32, job())
    def read(url, kind):
        if url == 'next':
            raise local_browser.CatalogNavigationError('Проверить предыдущую страницу')
        return url, '<main></main>'
    monkeypatch.setattr(runner, 'read_page', read)
    assert runner.run()[0] == 'completed'
    rechecks = [body for path, body in api.calls if path.endswith('/catalog')]
    assert [body['range_number'] for body in rechecks] == [2, 3]


@pytest.mark.parametrize('final_url', [
    'https://www.avito.ru/irkutsk/kvartiry/other_456',
    'https://www.avito.ru/irkutsk/kvartiry/prodam',
    'https://example.test/irkutsk/kvartiry/foo_123',
])
def test_redirected_not_found_is_retryable_and_never_confirms_removal(monkeypatch, final_url):
    from types import SimpleNamespace

    clock = Clock()
    class RedirectPage(ManualPage):
        def goto(self, url, **options):
            super().goto(final_url, **options)
            return SimpleNamespace(status=404, url=final_url)
    monkeypatch.setattr(local_browser.time, 'monotonic', clock.monotonic)
    runner = BrowserJob(FakeAPI(), RedirectPage(clock), 'a' * 32, job())
    with pytest.raises(ListingReadError, match='перенаправил'):
        runner.read_page('https://www.avito.ru/irkutsk/kvartiry/foo_123', 'listing')
