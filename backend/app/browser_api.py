import re
import logging
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Session

from app.browser_jobs import expire_browser_runs
from app.browser_protocol import LEASE_LOST, MAX_LISTING_READ_ATTEMPTS, PROTOCOL_VERSION
from app.config import settings
from app.catalog_navigation import catalog_url
from app.crawl import next_task, store_catalog, recover_catalog_range
from app.crawl_results import archive_item, finish_item, record_read_failure
from app.db import get_session
from app.models import Crawl, CrawlItem, CrawlRange, CollectionRun, utcnow
from app.parser import CATALOG_URL, ListingClosed, ListingSkipped, ParseError, canonical_url, parse_catalog, parse_listing
from app.repository import StaleObservation, save_listing
from app.cities import selected_city
from app.property_types import CATEGORIES
from app.parser import parse_catalog_count
from app.sampling import QUOTA, store_probe

log = logging.getLogger(__name__)


def local_collector(request: Request):
    origin = request.headers.get("origin")
    if (request.headers.get("x-local-collector") != "1"
            or (origin and urlsplit(origin).netloc != request.headers.get("host"))):
        raise HTTPException(403, "Доступ разрешён только локальному сборщику.")
    if settings().collector_mode != "browser":
        raise HTTPException(409, "Локальный браузерный режим отключён в настройках.")


router = APIRouter(prefix="/api/browser", dependencies=[Depends(local_collector)])


class Token(BaseModel):
    token: str = Field(pattern=r"^[a-f0-9]{32}$")


class Progress(Token):
    message: str = Field(default="Сбор в локальном браузере…", max_length=1000)


class Claim(Token):
    protocol_version: int = 1


class Page(Token):
    url: str = Field(max_length=10000)
    html: str = Field(max_length=8_000_000)
    page_number: int = Field(default=1, ge=1)
    recheck: bool = False
    range_number: int = Field(default=1, ge=1)


class ProbePage(Page):
    maximum: int | None = Field(default=None, ge=1)


class CatalogRecovery(Page):
    page_number: int = Field(ge=2)
    actual_url: str = Field(max_length=10000)
    actual_page: Literal[1] = 1


class Finish(Progress):
    status: Literal["completed", "partial", "blocked", "failed", "interrupted"]


class Unavailable(Token):
    url: str = Field(max_length=10000)
    actual_url: str | None = Field(default=None, max_length=10000)
    status: Literal[404, 410]


class ReadFailure(Token):
    url: str = Field(max_length=10000)
    attempt: int = Field(ge=1, le=MAX_LISTING_READ_ATTEMPTS)
    message: str = Field(min_length=1, max_length=1000)


def check_lease(run, token):
    if run is None or run.status != "running" or run.browser_token != token:
        raise HTTPException(409, LEASE_LOST)


def owned_run(session: Session, run_id: int, token: str) -> CollectionRun:
    run = session.scalar(select(CollectionRun).where(CollectionRun.id == run_id).with_for_update()
                         .execution_options(populate_existing=True))
    check_lease(run, token)
    run.heartbeat_at = utcnow()
    return run


def check_size(html: str):
    if len(html.encode("utf-8")) > 8_000_000:
        raise HTTPException(413, "HTML-страница слишком большая.")


@router.post("/claim")
def claim(body: Claim, session: Session = Depends(get_session)):
    if body.protocol_version not in {3, 4, 5, 6, PROTOCOL_VERSION}:
        raise HTTPException(409, "Сборщик обновлён. Закройте его и снова откройте «Запустить сборщик.command».")
    # Serialize two local collectors trying to claim the same durable job.
    session.execute(select(func.pg_advisory_xact_lock(765031894473)))
    expire_browser_runs(session)
    run = session.scalar(select(CollectionRun).where(
        CollectionRun.status == "running", CollectionRun.browser_token == body.token).with_for_update())
    if run is None:
        run = session.scalar(select(CollectionRun).where(CollectionRun.status == "queued")
                             .order_by(CollectionRun.id).with_for_update(skip_locked=True).limit(1))
    if run is None:
        session.commit()
        return {"run_id": None}
    crawl = session.get(Crawl, run.crawl_id)
    city = selected_city(session, crawl.city_id if crawl else None)
    if body.protocol_version == 3 and (city.slug != "irkutsk" or crawl and crawl.mode in {"sample", "refresh"}):
        raise HTTPException(409, "Для города и выборки 1000 перезапустите «Запустить сборщик.command»: нужен обновлённый сборщик.")
    if crawl and crawl.property_kind == 'house' and body.protocol_version < 5:
        raise HTTPException(409, 'Для сбора домов перезапустите «Запустить сборщик.command».')
    run.status, run.browser_token, run.heartbeat_at = "running", body.token, utcnow()
    run.started_at = run.started_at or utcnow()
    run.message = "Сбор в локальном браузере. При появлении проверки пройдите её в открытом окне."
    session.commit()
    config = settings()
    return {"run_id": run.id, "protocol_version": body.protocol_version, "city_slug": city.slug,
            "property_kind": crawl.property_kind if crawl else "apartment",
            "collection_mode": crawl.mode if crawl else "full", "catalog_recovery": body.protocol_version >= 6,
            "request_delay_seconds": config.request_delay_seconds,
            "max_pages": config.max_pages, "max_listings": config.max_listings,
            "request_timeout_seconds": config.request_timeout_seconds,
            "browser_wait_seconds": config.browser_wait_seconds}


@router.post("/runs/{run_id}/next")
def next_work(run_id: int, body: Token, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    try:
        task = next_task(session, run)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return task


@router.post("/runs/{run_id}/heartbeat")
def heartbeat(run_id: int, body: Progress, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    run.message = body.message
    session.commit()
    return {"ok": True}


def valid_catalog_url(url, city, property_kind):
    try:
        parts = urlsplit(url)
        return (parts.scheme == 'https' and parts.hostname in {'avito.ru', 'www.avito.ru'}
                and not parts.username and not parts.password and parts.port in {None, 443}
                and re.fullmatch(rf'/{re.escape(city.slug)}/{CATEGORIES[property_kind]}/prodam(?:-[A-Za-z0-9]+)?/?', parts.path))
    except ValueError:
        return False


@router.post('/runs/{run_id}/catalog-unreachable')
def catalog_unreachable(run_id: int, body: CatalogRecovery, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    check_size(body.html)
    crawl = session.get(Crawl, run.crawl_id)
    city = selected_city(session, crawl.city_id if crawl else None)
    if crawl is None or not all(valid_catalog_url(url, city, crawl.property_kind) for url in (body.url, body.actual_url)):
        raise HTTPException(422, 'Ожидался каталог выбранного рынка и города.')
    try:
        # CAPTCHA, an unknown page or dropped filters cannot complete or split a range.
        parse_catalog(body.html, body.actual_page, unknown_next=None, source_url=body.actual_url)
        result = recover_catalog_range(session, run, body.url, body.page_number, body.range_number, body.actual_url)
    except ParseError as exc:
        raise HTTPException(422, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return result


@router.post("/runs/{run_id}/catalog")
def catalog(run_id: int, body: Page, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    check_size(body.html)
    crawl = session.get(Crawl, run.crawl_id)
    city = selected_city(session, crawl.city_id)
    if not valid_catalog_url(body.url, city, crawl.property_kind):
        raise HTTPException(422, "Ожидалась страница каталога выбранного рынка и города.")
    try:
        urls, has_next = parse_catalog(body.html, body.page_number, unknown_next=None, source_url=body.url)
        store_catalog(session, run, catalog_url(body.url, body.page_number), body.page_number, urls, has_next,
                      body.recheck, body.range_number)
    except ParseError as exc:
        raise HTTPException(422, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return {"urls": urls, "has_next": has_next}


@router.post("/runs/{run_id}/probe")
def probe(run_id: int, body: ProbePage, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    check_size(body.html)
    crawl = session.get(Crawl, run.crawl_id)
    if crawl is None or crawl.mode != "sample":
        raise HTTPException(409, "Этот запуск не является выборкой.")
    parts, expected = urlsplit(body.url), urlsplit(crawl.catalog_url)
    if (parts.scheme != "https" or parts.netloc not in {"avito.ru", "www.avito.ru"}
            or not re.fullmatch(re.escape(expected.path) + r"(?:-[A-Za-z0-9]+)?/?", parts.path)):
        raise HTTPException(422, "Ожидался каталог выбранного города.")
    try:
        accepted = store_probe(session, crawl, run, body.url, body.maximum, parse_catalog_count(body.html))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    session.commit()
    return {"ok": True} if accepted else {"ok": True, "retry": True}


def queued_item(session, crawl, url):
    canonical = canonical_url(url, crawl.catalog_url if crawl else CATALOG_URL)
    if not canonical:
        raise HTTPException(422, "Ожидалось объявление выбранного типа недвижимости.")
    item = session.get(CrawlItem, (crawl.id, canonical.rsplit("_", 1)[-1])) if crawl else None
    if item is None:
        raise HTTPException(409, "Объявления нет в очереди сборщика.")
    return item


def already_processed(item):
    return {"saved": item.status == "saved", "message": item.error or "Объявление уже обработано."}


@router.post("/runs/{run_id}/listing")
def listing(run_id: int, body: Page, session: Session = Depends(get_session)):
    check_size(body.html)
    run = session.get(CollectionRun, run_id)
    check_lease(run, body.token)
    crawl = session.get(Crawl, run.crawl_id)
    item = queued_item(session, crawl, body.url)
    if item.status != "pending":
        return already_processed(item)
    city = selected_city(session, crawl.city_id)
    city_id, mode = city.id, crawl.mode
    parse_options = dict(city_name=city.name, city_slug=city.slug, property_kind=crawl.property_kind,
        city_center=(city.latitude, city.longitude) if city.latitude is not None and city.longitude is not None else None,
        radius_km=crawl.radius_km if crawl.property_kind == "house" else None)
    session.rollback()  # Release the connection while parsing; acquire the lease again before any write.
    observed_at = utcnow()
    outcome = None
    try:
        data = parse_listing(body.html, body.url, now=observed_at, **parse_options)
    except ListingClosed as exc:
        outcome = ("archive", str(exc))
    except ListingSkipped as exc:
        outcome = ("skipped", str(exc))
    except ParseError as exc:
        outcome = ("error", str(exc))

    run = owned_run(session, run_id, body.token)
    crawl = session.get(Crawl, run.crawl_id)
    item = queued_item(session, crawl, body.url)
    if item.status != "pending":
        result = already_processed(item)
    else:
        result = store_listing_result(session, run, crawl, item, outcome, data if outcome is None else None,
                                      city_id, mode, observed_at)
    session.commit()
    return result


def store_listing_result(session, run, crawl, item, outcome, data, city_id, mode, observed_at):
    if mode == "sample":
        count = session.scalar(select(func.count()).select_from(CrawlItem).where(
            CrawlItem.crawl_id == crawl.id, CrawlItem.range_number == item.range_number, CrawlItem.status == "saved"))
        if count >= QUOTA:
            finish_item(run, item, "not_selected")
            return {"saved": False, "message": f"В этой ценовой группе уже сохранено {QUOTA} объявлений."}
    if outcome:
        status, reason = outcome
        return (archive_item(session, run, item, reason, observed_at) if status == "archive"
                else finish_item(run, item, status, reason))
    if mode == "sample":
        part = session.get(CrawlRange, (crawl.id, item.range_number))
        if data.price < part.minimum or (part.maximum is not None and data.price > part.maximum):
            return finish_item(run, item, "skipped", "Цена объявления вышла из выбранной ценовой группы. Сбор продолжается.")
    try:
        with session.begin_nested():
            row, changed = save_listing(session, data, run.id, observed_at=observed_at, city_id=city_id,
                                        merge_missing=mode == "refresh")
    except StaleObservation as exc:
        return finish_item(run, item, "error", str(exc))
    except (IntegrityError, DataError) as exc:
        # Missing references, broken constraints elsewhere and connection failures are operational errors.
        state = getattr(exc.orig, "sqlstate", None)
        table = getattr(getattr(exc.orig, "diag", None), "table_name", None)
        if state not in {"22001", "22003", "22021"} and not (
                state == "23514" and table in {"listings", "listing_states", "price_observations"}):
            raise
        log.warning("run=%s listing=%s stage=store sqlstate=%s table=%s", run.id, item.avito_id, state, table)
        return finish_item(run, item, "error", "Данные объявления выходят за допустимые пределы хранения.")
    finish_item(run, item, "saved")
    run.saved += 1
    run.price_changes += int(changed)
    run.message = f"Сохранено {run.saved}; изменений цены: {run.price_changes}."
    return {"saved": True, "id": row.id, "price_changed": changed}


@router.post("/runs/{run_id}/listing-error")
def listing_error(run_id: int, body: ReadFailure, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    crawl = session.get(Crawl, run.crawl_id)
    item = queued_item(session, crawl, body.url)
    result = record_read_failure(run, item, body.attempt, body.message)
    session.commit()
    return result


@router.post("/runs/{run_id}/finish")
def finish(run_id: int, body: Finish, session: Session = Depends(get_session)):
    run = owned_run(session, run_id, body.token)
    crawl = session.get(Crawl, run.crawl_id) if run.crawl_id else None
    incomplete = body.status == "completed" and (crawl is None or crawl.status != "completed")
    run.status = "partial" if body.status == "completed" and (run.errors or incomplete) else body.status
    run.message = body.message
    if incomplete:
        run.message += " Обход ещё не окончен. Прогресс сохранён; нажмите «Продолжить сбор»."
    if run.errors:
        run.message += f" Пропущено объявлений: {run.errors}."
    run.finished_at, run.browser_token = utcnow(), None
    log.info("run=%s stage=finish outcome=%s errors=%s archived=%s", run.id, run.status, run.errors, run.archived)
    session.commit()
    return {"ok": True, "status": run.status}


@router.post("/runs/{run_id}/unavailable")
def unavailable(run_id: int, body: Unavailable, session: Session = Depends(get_session)):
    if body.actual_url is None:
        raise HTTPException(409, "Для проверки итогового адреса перезапустите «Запустить сборщик.command».")
    run = owned_run(session, run_id, body.token)
    crawl = session.get(Crawl, run.crawl_id)
    item = queued_item(session, crawl, body.url)
    # An HTTP status belongs to the last response, not to any earlier redirect.
    if not body.actual_url.startswith("https://"):
        raise HTTPException(422, "Не подтверждён итоговый адрес объявления.")
    actual = canonical_url(body.actual_url, crawl.catalog_url)
    if not actual or actual.rsplit("_", 1)[-1] != item.avito_id:
        raise HTTPException(422, "Не подтверждён итоговый адрес объявления.")
    if item.status == "pending":
        archive_item(session, run, item, f"Объявление недоступно (HTTP {body.status}). Сбор продолжается.")
    session.commit()
    return {"ok": True}
