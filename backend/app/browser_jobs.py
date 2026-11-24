from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.models import CollectionRun, Crawl, utcnow

LEASE_SECONDS = 120
RUN_START_LOCK = 765031894474


class PublicRun(BaseModel):
    id: int
    status: Literal["queued", "running", "completed", "partial", "blocked", "failed", "interrupted"]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    pages: int
    found: int
    saved: int
    errors: int
    price_changes: int
    archived: int
    message: str


def expire_browser_runs(session: Session):
    now = utcnow()
    session.execute(update(CollectionRun).where(
        CollectionRun.status == "running",
        or_(CollectionRun.browser_token.is_(None), CollectionRun.heartbeat_at.is_(None),
            CollectionRun.heartbeat_at < now - timedelta(seconds=LEASE_SECONDS)),
    ).values(status="interrupted", finished_at=now, browser_token=None,
             message="Локальный сборщик отключился. Данные и очередь сохранены; нажмите «Продолжить сбор»."))


def public_run(run: CollectionRun | None):
    if run is None:
        return None
    return {name: getattr(run, name) for name in (
        "id", "status", "created_at", "started_at", "finished_at", "pages", "found", "saved",
        "errors", "price_changes", "archived", "message",
    )}


def latest_collection(session, city_id=None, property_kind=None):
    query = select(CollectionRun)
    if city_id is not None or property_kind is not None:
        rules = []
        if city_id is not None:
            rules.append(Crawl.city_id == city_id)
        if property_kind is not None:
            rules.append(Crawl.property_kind == property_kind)
        query = query.outerjoin(Crawl, Crawl.id == CollectionRun.crawl_id)
        from app.cities import default_city
        http_home = property_kind in {None, 'apartment'} and (city_id is None or city_id == default_city(session).id)
        query = query.where(or_(and_(*rules), CollectionRun.crawl_id.is_(None)) if http_home else and_(*rules))
    return session.scalar(query.order_by(CollectionRun.id.desc()).limit(1))


def collection_data_version(session, city_id, property_kind):
    # The same house may be saved through one city and visible through another.
    run = latest_collection(session) if property_kind == 'house' else latest_collection(session, city_id, property_kind)
    return data_version(run)


def data_version(run):
    return f'{run.id}:{run.saved}:{run.archived}' if run else ''
