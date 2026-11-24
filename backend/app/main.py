import logging
import threading
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import Literal
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.collector import worker
from app.browser_api import router as browser_router
from app.browser_jobs import PublicRun, data_version, latest_collection, public_run
from app.config import settings
from app.crawl import progress, resumable_modes, start_browser_run
from app.db import get_session
from app.property_types import PropertyKind
from app.models import CollectionRun, Listing, PriceObservation
from app.listings import router as listings_router
from app.analytics import router as analytics_router
from app.duplicate_reviews import router as duplicates_router
from app.cities import router as cities_router
from app.maps import router as maps_router
from app.map_index import worker as map_worker
from app.repository import create_run

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app):
    stop = threading.Event()
    thread = None
    map_thread = None
    if settings().worker_enabled:
        thread = threading.Thread(target=worker, args=(stop,), daemon=True, name="collector")
        thread.start()
        map_thread = threading.Thread(target=map_worker, args=(stop,), daemon=True, name="map-import")
        map_thread.start()
    yield
    stop.set()
    if thread:
        thread.join(timeout=35)
    if map_thread:
        map_thread.join(timeout=2)


app = FastAPI(title="Housing Scraper", lifespan=lifespan)
app.include_router(browser_router)
app.include_router(listings_router)
app.include_router(analytics_router)
app.include_router(duplicates_router)
app.include_router(cities_router)
app.include_router(maps_router)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    reasons = {"int_parsing": "ожидалось целое число", "decimal_parsing": "ожидалось число",
               "greater_than_equal": "значение ниже допустимого", "less_than_equal": "значение выше допустимого",
               "literal_error": "недопустимый вариант", "missing": "обязательное поле"}
    errors = []
    for error in exc.errors():
        field = '.'.join(map(str, error['loc'][1:]))
        cause = error.get('ctx', {}).get('error')
        reason = str(cause) if isinstance(cause, ValueError) else reasons.get(error['type'], 'некорректное значение')
        errors.append({"field": field, "reason": reason})
    first = errors[0] if errors else {"field": "", "reason": "некорректное значение"}
    detail = f"{first['field']}: {first['reason']}." if first['field'] else first['reason']
    return JSONResponse(status_code=422, content={"detail": detail, "errors": errors})


@app.get("/api/health")
def health(session: Session = Depends(get_session)):
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.get("/api/listings/{listing_id}/prices")
def price_history(listing_id: int, session: Session = Depends(get_session)):
    listing = session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(404, "Объявление не найдено.")
    prices = session.scalars(select(PriceObservation).where(PriceObservation.listing_id == listing_id)
                             .order_by(PriceObservation.observed_at, PriceObservation.id)).all()
    items = []
    previous = None
    for price in prices:
        items.append({"id": price.id, "amount": price.amount, "observed_at": price.observed_at,
                      "source_updated_at": price.source_updated_at,
                      "change": price.amount - previous if previous is not None else None})
        previous = price.amount
    return {"listing_id": listing_id, "avito_id": listing.avito_id, "title": listing.title,
            "url": listing.url, "last_seen_at": listing.last_seen_at, "items": items}


class StartRun(BaseModel):
    mode: Literal["resume", "new"] = "resume"
    city_id: int | None = Field(default=None, ge=1)
    collection_mode: Literal["full", "sample", "refresh"] = "full"
    property_kind: PropertyKind = "apartment"


@app.get("/api/progress")
def collection_progress(city_id: int | None = Query(None, ge=1), property_kind: PropertyKind = "apartment",
                        collection_mode: Literal['full', 'sample', 'refresh'] | None = None,
                        session: Session = Depends(get_session)):
    return progress(session, city_id, property_kind, collection_mode)


@app.get("/api/collection-status")
def collection_status(city_id: int = Query(ge=1), property_kind: PropertyKind = "apartment",
                      collection_mode: Literal['full', 'sample', 'refresh'] | None = None,
                      since: str = Query(default='', max_length=180), session: Session = Depends(get_session)):
    run = latest_collection(session, city_id, property_kind)
    version = (f'{run.id}:{run.status}:{run.pages}:{run.found}:{run.saved}:{run.archived}:{run.errors}:{run.queue_revision}'
               if run else 'empty') + f':{collection_mode or ""}'
    global_run = latest_collection(session)
    result = {"run": public_run(run), "global_run": public_run(global_run),
              "version": version, "data_version": data_version(global_run if property_kind == 'house' else run)}
    if version != since:
        result.update(progress=progress(session, city_id, property_kind, collection_mode),
                      resumable=resumable_modes(session, city_id, property_kind))
    return result


@app.post("/api/runs", status_code=202, response_model=PublicRun)
def start_collection(request: Request, body: StartRun | None = None, session: Session = Depends(get_session)):
    origin = request.headers.get("origin")
    if origin and urlsplit(origin).netloc != request.headers.get("host"):
        raise HTTPException(403, "Запуск разрешён только из интерфейса этого приложения.")
    try:
        if settings().collector_mode == "browser":
            run = start_browser_run(session, fresh=body is not None and body.mode == "new",
                                    city_id=body.city_id if body else None,
                                    collection_mode=body.collection_mode if body else "full",
                                    property_kind=body.property_kind if body else "apartment")
        else:
            from app.cities import selected_city
            if body and (body.property_kind != "apartment" or body.collection_mode != "full" or body.city_id is not None and selected_city(session, body.city_id).name != "Иркутск"):
                raise ValueError("Для выбора города и выборки 1000 включите локальный браузерный режим.")
            run = create_run(session)
        session.commit()
        return public_run(run)
    except IntegrityError:
        session.rollback()
        raise HTTPException(409, "Сбор уже запущен. Дождитесь его завершения.")
    except ValueError as exc:
        session.rollback()
        raise HTTPException(409, str(exc))


@app.get("/api/runs/latest", response_model=PublicRun | None)
def latest_run(city_id: int | None = Query(None, ge=1), property_kind: PropertyKind | None = None, session: Session = Depends(get_session)):
    return public_run(latest_collection(session, city_id, property_kind))
