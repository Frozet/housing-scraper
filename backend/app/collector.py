import logging
import threading
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import select, text, update

from app.config import Settings, settings
from app.browser_jobs import expire_browser_runs
from app.catalog_navigation import catalog_url
from app.db import SessionLocal, engine
from app.models import CollectionRun, utcnow
from app.parser import CATALOG_URL, ListingSkipped, ParseError, client_redirect, html_document, parse_catalog, parse_listing
from app.repository import StaleObservation, save_listing

logger = logging.getLogger(__name__)
WORKER_LOCK = 765031894472


class CollectionError(Exception):
    pass


class Blocked(CollectionError):
    pass


class Interrupted(CollectionError):
    pass


class Unavailable(CollectionError):
    pass


def check_response(response: httpx.Response, document=None):
    # 439 is an Avito-specific refusal observed in production, not proof of an IP country.
    if response.status_code in (401, 403, 429, 439):
        raise Blocked(f"Авито ограничил доступ (HTTP {response.status_code}). Сбор остановлен; сохранённые данные доступны. Повторите запуск позже после восстановления доступа.")
    if response.status_code == 407:
        raise CollectionError("Прокси отклонил авторизацию. Проверьте логин, пароль и срок действия прокси.")
    if response.status_code in (404, 410):
        raise Unavailable("Объявление снято или недоступно.")
    if response.status_code >= 400:
        raise CollectionError(f"Авито вернул HTTP {response.status_code}.")
    soup = document if document is not None else html_document(response.text)
    title = soup.title.get_text(" ").lower() if soup.title else ""
    text_content = soup.get_text(" ", strip=True).lower()
    if (any(s in title for s in ("доступ ограничен", "captcha", "проверка браузера"))
            or soup.select_one('#captcha, [data-marker="captcha"]')
            or "подтвердите, что вы не робот" in text_content):
        raise Blocked("Авито запросил проверку доступа. Сбор остановлен; сохранённые данные доступны.")


def configured_proxy(config: Settings) -> str | None:
    if config.avito_proxy_url is None:
        if config.avito_proxy_required:
            raise CollectionError("Прокси не настроен. Перед запуском укажите адрес российского прокси.")
        return None
    value = config.avito_proxy_url.get_secret_value().strip()
    try:
        parts = urlsplit(value)
        valid = (parts.scheme in {"http", "https", "socks5", "socks5h"}
                 and parts.hostname and parts.port and 1 <= parts.port <= 65535
                 and parts.path in {"", "/"} and not parts.query and not parts.fragment
                 and not any(character.isspace() for character in value)
                 and ((parts.username is None and parts.password is None)
                      or (bool(parts.username) and bool(parts.password))))
    except ValueError:
        valid = False
    if not valid:
        # Never include the supplied URL: it can contain the proxy password.
        raise CollectionError("Некорректный адрес прокси. Нужен HTTP(S) или SOCKS5 URL с хостом и портом; логин и пароль указываются вместе.")
    return value


class Fetcher:
    def __init__(self, config: Settings, stop: threading.Event):
        self.config, self.stop = config, stop
        self.last_url = None
        proxy = configured_proxy(config)
        self.uses_proxy = proxy is not None
        try:
            self.client = httpx.Client(proxy=proxy, trust_env=False,
                                       timeout=config.request_timeout_seconds, follow_redirects=False,
                                       headers={"User-Agent": "ApartmentPriceTracker/1.0", "Accept-Language": "ru-RU,ru;q=0.9"})
        except ImportError:
            raise CollectionError("Поддержка SOCKS5 не установлена. Пересоберите backend с requirements-proxy.txt.") from None
        except (httpx.InvalidURL, ValueError):
            raise CollectionError("Не удалось настроить прокси. Проверьте формат адреса и кодирование логина и пароля.") from None

    def close(self):
        self.client.close()

    def pause(self, seconds):
        if self.stop.wait(seconds):
            raise Interrupted("Сбор прерван при остановке приложения. Сохранённые данные доступны.")

    def get(self, url: str) -> str:
        self.last_document = None
        self.pause(self.config.request_delay_seconds)
        for attempt in range(3):
            try:
                target = url
                visited_redirects = set()
                for redirect_number in range(6):
                    parts = urlsplit(target)
                    if (parts.scheme != "https" or parts.hostname not in {"avito.ru", "www.avito.ru"}
                            or parts.username or parts.password or parts.port not in (None, 443)
                            or not parts.path.startswith("/irkutsk/kvartiry/")):
                        raise CollectionError("Авито перенаправил запрос за пределы раздела квартир Иркутска.")
                    if target in visited_redirects:
                        raise CollectionError("Авито вернул циклическое перенаправление.")
                    visited_redirects.add(target)
                    if redirect_number:
                        self.pause(self.config.request_delay_seconds)
                    response = self.client.get(target)
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise CollectionError("Авито вернул HTTP-перенаправление без адреса назначения.")
                    else:
                        if response.status_code >= 500:
                            break
                        document = html_document(response.text) if response.status_code < 400 else None
                        check_response(response, document)
                        location = client_redirect(document)
                        if not location:
                            self.last_url = target
                            self.last_document = document
                            return response.text
                    target = urljoin(target, location)
                else:
                    raise CollectionError("Слишком много перенаправлений Авито.")
                if response.status_code >= 500 and attempt < 2:
                    self.pause(2 ** (attempt + 1))
                    continue
                check_response(response)
                raise CollectionError("Не удалось загрузить страницу Авито.")
            except httpx.ProxyError:
                raise CollectionError("Ошибка подключения или авторизации прокси. Проверьте адрес, порт, логин, пароль и срок аренды.") from None
            except httpx.RequestError:
                if attempt == 2:
                    route = " через настроенный прокси" if self.uses_proxy else ""
                    raise CollectionError(f"Не удалось получить ответ Авито{route} после трёх попыток. Проверьте соединение.") from None
                self.pause(2 ** (attempt + 1))
        raise CollectionError("Не удалось получить страницу Авито.")


def update_run(run_id: int, **values):
    with SessionLocal.begin() as session:
        session.execute(update(CollectionRun).where(CollectionRun.id == run_id).values(**values))


def collect(run_id: int, stop: threading.Event, config: Settings | None = None, fetcher=None):
    config = config or settings()
    owned_fetcher = fetcher is None
    visited, catalog_fingerprints = set(), set()
    pages = errors = saved = changes = found = 0
    status, message = "completed", "Сбор завершён."
    try:
        if fetcher is None:
            fetcher = Fetcher(config, stop)
        page_number = 1
        catalog_base = CATALOG_URL
        while True:
            url = catalog_url(catalog_base, page_number, keep_blank_values=False)
            html = fetcher.get(url)
            if effective_url := getattr(fetcher, "last_url", None):
                catalog_base = effective_url
            urls, has_next = parse_catalog(getattr(fetcher, 'last_document', None) or html, page_number)
            fingerprint = frozenset(urls)
            if urls and fingerprint in catalog_fingerprints:
                raise CollectionError("Авито повторно вернул ту же страницу каталога. Проверьте доступ или разметку пагинации.")
            catalog_fingerprints.add(fingerprint)
            pages += 1
            new_urls = [url for url in urls if url not in visited]
            found += len(new_urls)
            update_run(run_id, pages=pages, found=found, message=f"Обработка страницы {page_number}.")
            for detail_url in new_urls:
                if config.max_listings and len(visited) >= config.max_listings:
                    status, message = "partial", "Достигнут заданный лимит объявлений."
                    break
                visited.add(detail_url)
                try:
                    detail_html = fetcher.get(detail_url)
                    data = parse_listing(getattr(fetcher, 'last_document', None) or detail_html, detail_url)
                    with SessionLocal.begin() as session:
                        _, changed = save_listing(session, data, run_id)
                        saved += 1
                        changes += int(changed)
                        session.execute(update(CollectionRun).where(CollectionRun.id == run_id).values(
                            saved=saved, price_changes=changes, message=f"Сохранено {saved}; изменений цены: {changes}."))
                except ListingSkipped as exc:
                    update_run(run_id, message=str(exc))
                    continue
                except (ParseError, StaleObservation, Unavailable) as exc:
                    errors += 1
                    logger.warning("Объявление %s: %s", detail_url, exc)
                    update_run(run_id, errors=errors, message=f"Пропущено объявление: {exc}")
            if status == "partial" or not has_next:
                break
            if config.max_pages and pages >= config.max_pages:
                status, message = "partial", "Достигнут заданный лимит страниц."
                break
            page_number += 1
        if errors and status == "completed":
            status, message = "partial", f"Сбор завершён с пропусками: {errors}. Подробности в журнале приложения."
    except Blocked as exc:
        status, message = "blocked", str(exc)
    except Interrupted as exc:
        status, message = "interrupted", str(exc)
    except (CollectionError, ParseError) as exc:
        status, message = "failed", str(exc)
    except Exception:
        logger.exception("Ошибка сбора %s", run_id)
        status, message = "failed", "Внутренняя ошибка сбора. Подробности в журнале приложения; сохранённые данные доступны."
    finally:
        if owned_fetcher and fetcher is not None:
            fetcher.close()
        update_run(run_id, status=status, message=message, finished_at=utcnow(), errors=errors)


def run_once(stop: threading.Event | None = None) -> bool:
    stop = stop or threading.Event()
    # Session-level lock belongs to this connection and is always released before pooling.
    # This lets web workers and the CLI share one durable queue without parallel crawls.
    with engine.connect() as connection:
        locked = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": WORKER_LOCK})
        connection.commit()
        if not locked:
            return False
        try:
            with SessionLocal.begin() as session:
                session.execute(update(CollectionRun).where(CollectionRun.status == "running").values(
                    status="interrupted", finished_at=utcnow(), message="Предыдущий процесс сбора был остановлен. Можно запустить повторный сбор."))
                run = session.scalar(select(CollectionRun).where(CollectionRun.status == "queued").order_by(CollectionRun.id).limit(1).with_for_update())
                if run is None:
                    return False
                run.status, run.started_at, run.message = "running", utcnow(), "Подключение к Авито…"
                run_id = run.id
            collect(run_id, stop)
            return True
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": WORKER_LOCK})
            connection.commit()


def worker(stop: threading.Event):
    while not stop.is_set():
        try:
            if settings().collector_mode == "browser":
                with SessionLocal.begin() as session:
                    expire_browser_runs(session)
            else:
                run_once(stop)
        except Exception:
            logger.exception("Ошибка фонового обработчика")
        stop.wait(2)
