"""User-run local browser. Backend receives HTML; database credentials stay in Docker."""
import argparse
from contextlib import contextmanager
from dataclasses import dataclass
import errno
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import secrets
import time
import traceback
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, ProxyHandler

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from app.catalog_navigation import CURRENT_PAGE_SELECTOR, catalog_page, page_in_url
from app.browser_protocol import LEASE_LOST, PROTOCOL_VERSION
from app.markup import (
    ADDRESS_SELECTOR, LISTING_PARAM_ROWS_SELECTOR, LISTING_STATUS_HEADING_SELECTOR,
    LISTING_STATUS_MESSAGE_SELECTOR, listing_is_auction, listing_status,
)
from app.price_ranges import price_bounds, radius_bounds, canonical_filter_url, filter_label
from app.property_types import CATEGORIES

ROOT = Path(__file__).resolve().parents[2]
CATALOG_READY = '[data-marker="item"], [data-marker="search-results/empty"], [data-marker="search-results/empty-title"]'
PRICE_READY = 'meta[itemprop="price"], [data-marker="item-view/item-price"]'
LISTING_DELAY_FACTOR = 0.5
LISTING_ADDRESS_WAIT_SECONDS = 7
LOGGER = logging.getLogger(__name__)


def document_is_changing(error, *, navigation=False):
    message = str(error).casefold()
    return (any(text in message for text in (
        "execution context was destroyed", "cannot find context with specified id",
        "cannot find context with id", "page is navigating and changing the content",
        "frame was detached", "frame has been detached",
    )) or navigation and ("net::err_aborted" in message or "interrupted by another navigation" in message))


def diagnostic_text(value):
    # Error messages can contain tracking queries; never persist those or URL credentials.
    def clean_url(match):
        try:
            parts = urlsplit(match[0])
            return urlunsplit((parts.scheme, parts.hostname or "", parts.path, "", ""))
        except ValueError:
            return "[адрес скрыт]"
    return re.sub(r'https?://[^\s<>"\']+', clean_url, str(value))


def access_check(headings):
    return any(re.fullmatch(
        r"(?:проверка (?:браузера|доступа)|доступ (?:ограничен|временно ограничен)|"
        r"подтвердите,? что вы не робот|проверяем,? (?:что вы не робот|ваш браузер))\s*[.!…]*",
        re.sub(r"\s+", " ", heading).strip(), re.I,
    ) for heading in headings)


class ClientError(Exception):
    pass


class ApiTemporaryError(ClientError):
    pass


class LeaseLost(ClientError):
    pass


class ListingReadError(ClientError):
    pass


class CatalogLoadTimeout(ClientError):
    pass


class CatalogNavigationError(ClientError):
    def __init__(self, message, *, actual_url=None, actual_page=None, html=None):
        super().__init__(message)
        self.actual_url, self.actual_page, self.html = actual_url, actual_page, html


class BrowserStopped(Exception):
    pass


class ManualCheckTimeout(Exception):
    pass


class DocumentChanged(Exception):
    pass


class ListingUnavailable(Exception):
    def __init__(self, status, actual_url):
        self.status = status
        self.actual_url = actual_url


def default_api_url():
    port = os.environ.get("WEB_PORT", "8088")
    env = ROOT / ".env"
    if "WEB_PORT" not in os.environ and env.exists():
        match = re.search(r'^\s*WEB_PORT\s*=\s*[\'"]?(\d+)', env.read_text(encoding="utf-8-sig"), re.M)
        if match:
            port = match[1]
    return f"http://127.0.0.1:{port}/api"


class LocalAPI:
    def __init__(self, base_url):
        parts = urlsplit(base_url)
        if (parts.scheme != "http" or parts.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parts.username or parts.password or parts.query or parts.fragment
                or parts.path.rstrip("/") != "/api"):
            raise ClientError("Адрес приложения должен быть локальным HTTP-адресом с путём /api.")
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(ProxyHandler({}))

    def call(self, path, body=None):
        data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
        request = Request(self.base_url + path, data=data, headers={
            "Content-Type": "application/json", "X-Local-Collector": "1",
        })
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in {502, 503, 504}:
                raise ApiTemporaryError("Локальное приложение временно недоступно.") from None
            try:
                detail = json.load(exc).get("detail", "Ошибка локального приложения.")
            except (ValueError, AttributeError):
                detail = "Ошибка локального приложения."
            error = LeaseLost if exc.code == 409 and detail == LEASE_LOST else ClientError
            raise error(str(detail)) from None
        except (URLError, OSError, ValueError):
            raise ApiTemporaryError("Локальное приложение недоступно. Проверьте, что Docker-контейнеры запущены.") from None


def allowed_page(value, city_slug="irkutsk", property_kind="apartment", *, nearby=False):
    try:
        parts = urlsplit(value)
        return (parts.scheme == "https" and parts.hostname in {"www.avito.ru", "avito.ru"}
                and parts.port in {None, 443} and not parts.username and not parts.password
                and re.match(rf"/{'[a-z0-9_-]+' if nearby and property_kind == 'house' else re.escape(city_slug)}/{CATEGORIES[property_kind]}/", parts.path) is not None)
    except ValueError:
        return False


def _listing_id(url):
    match = re.fullmatch(r"/[^/]+/[^/]+/[^/]+_([0-9]+)/?", urlsplit(url).path)
    return match[1] if match else None


class _RetryCatalog(Exception):
    """The rendered catalog stayed on the wrong page or lost its filters."""


@dataclass
class _CatalogLoad:
    since: float | None = None
    changed: float | None = None
    count: int | None = None
    page_mismatch: float | None = None
    filter_mismatch: float | None = None


class BrowserJob:
    def __init__(self, api, page, token, job, navigation_timeouts=(TimeoutError,), browser_errors=()):
        self.api, self.page, self.token, self.job = api, page, token, job
        self.path = f'/browser/runs/{job["run_id"]}'
        self.last_heartbeat = 0
        self.navigation_timeouts = navigation_timeouts
        self.browser_errors = browser_errors
        self.current_url = None

    def send(self, suffix, **values):
        body = {"token": self.token, **values}
        # Catalog and probe writes can advance counters after a lost response.
        # Stop and let the durable queue decide the next task instead of replaying them.
        safe = suffix in {"/next", "/heartbeat", "/finish", "/listing", "/unavailable", "/listing-error"}
        for delay in (0, 1, 3, 6) if safe else (0,):
            if delay:
                self.page.wait_for_timeout(delay * 1000)
            try:
                return self.api.call(self.path + suffix, body)
            except ApiTemporaryError as exc:
                last_error = exc
        raise last_error

    def heartbeat(self, message, force=False):
        now = time.monotonic()
        if force or now - self.last_heartbeat >= 5:
            self.send("/heartbeat", message=message)
            self.last_heartbeat = now

    def wait(self, seconds, message):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            if self.page.is_closed():
                raise BrowserStopped
            self.heartbeat(message)
            self.page.wait_for_timeout(min(500, max(1, (until - time.monotonic()) * 1000)))

    def skippable_listing_page(self):
        headings = self.page.locator(LISTING_STATUS_HEADING_SELECTOR).all_text_contents()
        return (listing_status(headings, self.page.locator(LISTING_STATUS_MESSAGE_SELECTOR).all_text_contents()) is not None
                or listing_is_auction(headings, self.page.locator(LISTING_PARAM_ROWS_SELECTOR).all_text_contents()))

    def page_content(self):
        current_url = self.page.url
        html = self.page.content()
        if self.page.url != current_url:
            raise DocumentChanged
        return current_url, html

    def _allowed_page(self, url, kind):
        return allowed_page(
            url, self.job.get("city_slug", "irkutsk"),
            self.job.get("property_kind", "apartment"), nearby=kind == "listing",
        )

    def _open_page(self, url, kind):
        delay = self.job["request_delay_seconds"]
        if kind == "listing":
            delay = max(1, delay * LISTING_DELAY_FACTOR)
        self.wait(delay, "Подготовка следующей страницы…")
        self.heartbeat("Открывается страница в локальном браузере…", force=True)
        try:
            response = self.page.goto(url, wait_until="domcontentloaded",
                                      timeout=self.job["request_timeout_seconds"] * 1000)
            if kind == "listing" and response is not None and response.status in {404, 410}:
                actual = self.page.url
                if not all(self._same_listing(url, target) for target in (response.url, actual)):
                    raise ListingReadError("Не удалось подтвердить недоступность: Авито перенаправил на другой адрес.")
                if not self.skippable_listing_page():
                    if not access_check(self.page.locator("h1").all_text_contents()):
                        if self.page.url != actual:
                            return  # Recheck the new document in the normal wait loop.
                        raise ListingUnavailable(response.status, actual)
            return response.status if response is not None else None
        except self.navigation_timeouts:
            pass  # A manual challenge can still be visible after navigation times out.
        except self.browser_errors as exc:
            if not document_is_changing(exc, navigation=True):
                raise
            self.heartbeat("Ожидается завершение перехода страницы…", force=True)

    def _catalog_wait(self, url, load, retry):
        """Validate rendered filters and pagination before allowing a snapshot."""
        try:
            same_filters = (price_bounds(url) == price_bounds(self.page.url)
                            and radius_bounds(url) == radius_bounds(self.page.url))
        except ValueError:
            same_filters = False
        if not same_filters:
            now = time.monotonic()
            if load.filter_mismatch is None:
                load.filter_mismatch = now
            if now - load.filter_mismatch < 3:
                return "Ожидается восстановление цены и радиуса каталога…"
            if not retry:
                raise _RetryCatalog("Повторно открывается каталог с нужной ценой и радиусом…")
            try:
                actual = filter_label(self.page.url)
            except ValueError:
                actual = "не удалось прочитать фильтры"
            raise ClientError(
                "Авито сбросил фильтр цены или радиуса. "
                f"Ожидалось: {filter_label(url)}. Получено: {actual}. "
                "Повторное открытие не помогло. Прогресс сохранён."
            )
        load.filter_mismatch = None

        expected_page = page_in_url(url)
        actual_page = catalog_page(self.page.url, self.page.locator(CURRENT_PAGE_SELECTOR).all_text_contents())
        if expected_page != actual_page:
            now = time.monotonic()
            if load.page_mismatch is None:
                load.page_mismatch = now
            message = f"Ожидается страница {expected_page}; сейчас открыта {actual_page or 'неизвестная'}. Прогресс сохранён."
            if now - load.page_mismatch < 3:
                return message
            self.heartbeat(message)
            if not retry:
                raise _RetryCatalog("Повторно открывается нужная страница каталога…")
            actual_url, html = self.page_content()
            raise CatalogNavigationError(
                message + " Полнота обхода пока не подтверждена. Браузер оставлен открытым.",
                actual_url=actual_url, actual_page=actual_page, html=html,
            )
        load.page_mismatch = None

        now = time.monotonic()
        if load.since is None:
            load.since = now
            self.page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
        count = self.page.locator('[data-marker="item"]').count()
        if count != load.count:
            load.count, load.changed = count, now
        # The first cards can render before the remaining cards and footer.
        if now - load.changed < 2 and now - load.since < 10:
            return "Ожидается загрузка карточек и переходов каталога…"
        return None

    def _same_listing(self, url, actual):
        expected = _listing_id(url)
        return bool(expected and self._allowed_page(actual, "listing") and expected == _listing_id(actual))

    def _has_address(self):
        visible = any(value.strip() for value in self.page.locator(ADDRESS_SELECTOR).all_text_contents())
        metadata = self.page.locator('[itemprop~="streetAddress"][content]:not([content=""])').count() > 0
        return visible or metadata

    def read_page(self, url, kind):
        self.current_url = url
        if not self._allowed_page(url, kind):
            raise ClientError("Получен адрес за пределами выбранного рынка недвижимости.")
        if kind == "catalog":
            try:
                url = canonical_filter_url(url)
            except ValueError as exc:
                raise ClientError(str(exc)) from None
        try:
            return self._read_page_attempt(url, kind)
        except _RetryCatalog as exc:
            # Each page gets one fresh attempt; further recovery belongs to the queue.
            self.heartbeat(str(exc), force=True)
            return self._read_page_attempt(url, kind, retry=True)

    def _read_page_attempt(self, url, kind, *, retry=False):
        http_status = self._open_page(url, kind)
        until = time.monotonic() + self.job["browser_wait_seconds"]
        printed_message = None
        message = "Ожидается загрузка страницы…"
        manual_check = False
        details_until = None
        catalog_load = _CatalogLoad()
        while time.monotonic() < until:
            if self.page.is_closed():
                raise BrowserStopped
            try:
                skippable = kind == "listing" and self.skippable_listing_page()
                if kind == "catalog":
                    ready = self.page.locator(CATALOG_READY).count() > 0
                else:
                    has_title = self.page.locator("h1").count() > 0
                    has_price = self.page.locator(PRICE_READY).count() > 0
                    ready = skippable or (has_title and has_price)

                if ready and self._allowed_page(self.page.url, kind):
                    manual_check = False
                    if kind == "catalog":
                        wait_message = self._catalog_wait(url, catalog_load, retry)
                        if wait_message is None:
                            return self.page_content()
                        message = wait_message
                    else:
                        if not self._same_listing(url, self.page.url):
                            raise ListingReadError("В браузере открыто другое объявление.")
                        if details_until is None:
                            details_until = min(until, time.monotonic() + LISTING_ADDRESS_WAIT_SECONDS)
                        if skippable or self._has_address() or time.monotonic() >= details_until:
                            actual, html = self.page_content()
                            if not self._same_listing(url, actual):
                                raise DocumentChanged
                            return actual, html
                        message = "Ожидается загрузка адреса объявления…"
                else:
                    manual_check = access_check(self.page.locator("h1").all_text_contents())
                    if manual_check:
                        message = "Авито показывает проверку доступа. Пройдите её вручную в окне браузера."
                    elif ready:
                        message = "Ожидается возврат к странице выбранного города и рынка…"
                    elif kind == "listing":
                        missing = ", ".join(label for label, found in (
                            ("заголовок", has_title), ("цена", has_price),
                        ) if not found)
                        message = f"Ожидается загрузка объявления: пока не найдены {missing}."
                    else:
                        message = "Ожидается загрузка каталога…"
                    if printed_message != message:
                        print(message, flush=True)
                        printed_message = message
            except self.browser_errors + (DocumentChanged,) as exc:
                if self.page.is_closed():
                    raise BrowserStopped from None
                if not isinstance(exc, DocumentChanged) and not document_is_changing(exc):
                    raise
                # A new document must pass all checks again, within the same deadline.
                details_until = None
                catalog_load = _CatalogLoad()
                message = "Страница перезагружается. Ожидается завершение загрузки…"

            self.heartbeat(message)
            self.page.wait_for_timeout(500)

        if manual_check:
            raise ManualCheckTimeout("Авито показывает проверку доступа. Пройдите её вручную и продолжите сбор. "
                                     "Прогресс сохранён, браузер оставлен открытым.")
        if http_status in {401, 403, 429, 439}:
            raise ManualCheckTimeout(f"Авито ограничил доступ (HTTP {http_status}). Прогресс сохранён.")
        if kind == "listing":
            raise ListingReadError(f"Не удалось дождаться загрузки объявления №{_listing_id(url)}. {message}")
        raise CatalogLoadTimeout(f"Не удалось дождаться загрузки каталога. {message} "
                                 "Прогресс сохранён, браузер оставлен открытым.")

    def recover_catalog(self, task, error):
        # A reset is not proof that a range is complete. Narrow it only after the
        # server validates the actual catalog, city, price and radius again.
        if (not self.job.get('catalog_recovery') or self.job.get('collection_mode') != 'full'
                or task['kind'] != 'catalog' or task.get('page_number', 1) <= 1
                or error.actual_page != 1 or not error.actual_url or error.html is None):
            return False
        result = self.send('/catalog-unreachable', url=task['url'], page_number=task['page_number'],
                           range_number=task.get('range_number', 1), actual_url=error.actual_url,
                           actual_page=error.actual_page, html=error.html)
        print(result['message'], flush=True)
        return True

    def run(self):
        rechecked_catalogs = set()
        while True:
            task = self.send("/next")
            if task["kind"] == "done":
                return task["status"], task["message"]
            try:
                current_url, html = self.read_page(task["url"], task["kind"])
            except CatalogNavigationError as error:
                number = task.get("page_number")
                key = (task.get("range_number", 1), number)
                if task["kind"] != "catalog" or not task.get("previous_url") or key in rechecked_catalogs:
                    if self.recover_catalog(task, error):
                        continue
                    raise
                rechecked_catalogs.add(key)
                self.heartbeat(f"Проверяется, закончился ли каталог на странице {number - 1}…", force=True)
                try:
                    previous_url, previous_html = self.read_page(task["previous_url"], "catalog")
                except CatalogNavigationError:
                    if self.recover_catalog(task, error):
                        continue
                    raise error
                self.send("/catalog", url=previous_url, html=previous_html, page_number=number - 1,
                          range_number=task.get("range_number", 1), recheck=True)
                continue
            except ListingUnavailable as exc:
                self.send("/unavailable", url=task["url"], actual_url=exc.actual_url, status=exc.status)
                print("Объявление снято или недоступно. Сбор продолжается.", flush=True)
                continue
            except ListingReadError as exc:
                attempt = task.get("read_attempts", 0) + 1
                result = self.send("/listing-error", url=task["url"], attempt=attempt, message=str(exc))
                LOGGER.warning("run=%s listing=%s stage=read attempt=%s retry=%s reason=%s",
                               self.job["run_id"], _listing_id(task["url"]), attempt, result["retry"], exc)
                print(result["message"], flush=True)
                continue
            if task.get("purpose") == "sample_probe":
                self.send("/probe", url=current_url, html=html, maximum=task["maximum"])
            elif task["kind"] == "catalog":
                self.send("/catalog", url=current_url, html=html, page_number=task["page_number"],
                          range_number=task.get("range_number", 1))
            else:
                saved = self.send("/listing", url=current_url, html=html)
                print("Объявление сохранено." if saved["saved"] else saved["message"], flush=True)


def execute_job(runner):
    stopped = False
    try:
        status, message = runner.run()
    except ManualCheckTimeout as exc:
        status, message = "blocked", str(exc)
        LOGGER.warning("Запуск %s, страница %s: %s", runner.job["run_id"],
                       diagnostic_text(runner.current_url or runner.page.url), message)
    except (BrowserStopped, KeyboardInterrupt):
        status, message, stopped = "interrupted", "Локальный браузер закрыт или сбор остановлен пользователем.", True
    except (CatalogNavigationError, CatalogLoadTimeout) as exc:
        status, message = "partial", str(exc)
    except LeaseLost as exc:
        print(str(exc), flush=True)
        return True
    except ApiTemporaryError as exc:
        status, message = "partial", f"{exc} Прогресс сохранён; продолжите сбор после восстановления связи."
    except ClientError as exc:
        status, message = "failed", str(exc)
    except Exception as exc:
        if runner.page.is_closed():
            status, message, stopped = "interrupted", "Окно локального браузера закрыто.", True
        else:
            detail = diagnostic_text(exc).splitlines()[0][:350] if str(exc) else type(exc).__name__
            status, message = "failed", (f"Ошибка сбора ({type(exc).__name__}): {detail}. "
                                         "Прогресс сохранён, браузер оставлен открытым.")
            LOGGER.error("Запуск %s, страница %s\n%s", runner.job["run_id"],
                         diagnostic_text(runner.current_url or runner.page.url),
                         diagnostic_text(traceback.format_exc()))
    try:
        runner.send("/finish", status=status, message=message)
    except (ApiTemporaryError, LeaseLost) as exc:
        LOGGER.warning("run=%s stage=finish status=%s reason=%s", runner.job["run_id"], status, exc)
    print(message, flush=True)
    return not stopped


@contextmanager
def browser_lock(path):
    # Keep the lock file in place and let the OS release the lock when the handle closes.
    with path.open("ab") as lock:
        lock.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            yield False
        else:
            yield True


def main():
    parser = argparse.ArgumentParser(description="Бесплатный локальный браузерный сборщик. CAPTCHA проходит пользователь.")
    parser.add_argument("--api-url", default=default_api_url())
    parser.add_argument("--profile-dir", type=Path, default=ROOT / ".runtime" / "browser-profile")
    args = parser.parse_args()
    args.profile_dir.parent.mkdir(parents=True, exist_ok=True)
    with browser_lock(args.profile_dir.parent / "browser.lock") as acquired:
        if not acquired:
            print("Локальный сборщик уже запущен.")
            return
        handler = RotatingFileHandler(args.profile_dir.parent / "collector-errors.log", maxBytes=2_000_000,
                                      backupCount=2, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)
        api = LocalAPI(args.api_url)
        try:
            api.call("/health")
        except ClientError as exc:
            parser.exit(1, str(exc) + "\n")
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as NavigationTimeout, Error as BrowserError
        except ImportError:
            parser.exit(1, "Для установки Playwright запустите сборщик по инструкции в README.md.\n")
        token = secrets.token_hex(16)
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        with sync_playwright() as playwright:
            options = {"headless": False, "locale": "ru-RU", "viewport": {"width": 1280, "height": 900}}
            if chrome.exists():
                options["channel"] = "chrome"
            try:
                context = playwright.chromium.launch_persistent_context(str(args.profile_dir), **options)
            except BrowserError as exc:
                LOGGER.error("Не удалось открыть браузер: %s", diagnostic_text(exc))
                parser.exit(1, "Не удалось открыть браузер. Проверьте требования в BROWSER_SETUP.md. "
                               "Подробности в collector-errors.log рядом с папкой профиля.\n")
            try:
                page = context.pages[0] if context.pages else context.new_page()
                print("Сборщик готов. Нажмите «Запустить сбор» в веб-интерфейсе. Проверку Авито проходите вручную в отдельном окне.", flush=True)
                while not page.is_closed():
                    try:
                        job = api.call("/browser/claim", {"token": token, "protocol_version": PROTOCOL_VERSION})
                    except ApiTemporaryError:
                        print("Ожидается восстановление связи с локальным приложением…", flush=True)
                        page.wait_for_timeout(5000)
                        continue
                    if job["run_id"] is not None:
                        runner = BrowserJob(api, page, token, job, (NavigationTimeout,), (BrowserError,))
                        if not execute_job(runner):
                            break
                    else:
                        page.wait_for_timeout(2000)
            except KeyboardInterrupt:
                pass
            except ClientError as exc:
                print(str(exc), flush=True)
            except BrowserError:
                print("Окно браузера закрыто или браузер остановлен. При необходимости откройте сборщик снова.", flush=True)
            finally:
                context.close()


if __name__ == "__main__":
    main()
