import re
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select, or_
from sqlalchemy.orm import Session

from app.db import get_session
from app.browser_jobs import RUN_START_LOCK
from app.property_types import CATEGORIES, PropertyKind, house_radius
from app.price_ranges import RADIUS_ID, encode_fields
from app.models import Address, City, CollectionRun, Crawl, Listing, ListingCityScope, ListingStateScope

router = APIRouter(prefix="/api/cities")


def editable_city(request, session, city_id):
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403, 'Изменение разрешено только из интерфейса приложения.')
    # Serialize settings changes with the decision to start or resume a crawl.
    session.execute(select(func.pg_advisory_xact_lock(RUN_START_LOCK)))
    city = session.get(City, city_id)
    if city is None:
        raise HTTPException(404, 'Город не найден.')
    return city


def default_city(session):
    city = session.scalar(select(City).where(City.name == "Иркутск").order_by(City.id).limit(1))
    if city is None:
        city = City(name="Иркутск", region="Иркутская обл", slug="irkutsk")
        session.add(city)
        session.flush()
    return city


def selected_city(session, city_id=None):
    city = session.get(City, city_id) if city_id else default_city(session)
    if city is None:
        raise ValueError("Город не найден.")
    return city


def city_catalog(city, property_kind="apartment"):
    if not city.slug:
        raise ValueError("Для города не указан адрес каталога Авито.")
    if property_kind == 'house':
        from urllib.parse import urlencode
        query = urlencode({'f': encode_fields('house', [(RADIUS_ID, {'from': None, 'to': house_radius(city)})]), 'localPriority': '0'})
        return f'https://www.avito.ru/{city.slug}/doma_dachi_kottedzhi/prodam?{query}'
    return f"https://www.avito.ru/{city.slug}/kvartiry/prodam"


def listing_city_condition(city_id, listing=Listing):
    from app.models import ListingState
    scope = ListingCityScope if listing is Listing else ListingStateScope
    parent = scope.listing_id if listing is Listing else scope.state_id
    return or_(listing.address_id.in_(select(Address.id).where(Address.city_id == city_id)),
               select(parent).where(parent == listing.id, scope.city_id == city_id).exists())


@router.get("")
def cities(property_kind: PropertyKind = "apartment", session: Session = Depends(get_session)):
    counts = (select(Address.city_id.label("city_id"), func.count(Listing.id).label("count"))
              .join(Listing, Listing.address_id == Address.id).where(Listing.property_kind == property_kind).group_by(Address.city_id).subquery())
    if property_kind == 'house':
        counts = (select(ListingCityScope.city_id.label('city_id'), func.count().label('count'))
                  .group_by(ListingCityScope.city_id).subquery())
    rows = session.execute(select(City, func.coalesce(counts.c.count, 0)).outerjoin(counts, counts.c.city_id == City.id)).all()
    items = [{"id": city.id, "name": city.name, "region": city.region, "listing_count": count,
              "has_data": count > 0, "catalog_url": city_catalog(city, property_kind) if city.slug else "",
              "house_radius_km": house_radius(city), "radius_is_custom": city.house_radius_km is not None, "population": city.population} for city, count in rows]
    items.sort(key=lambda item: (not item["has_data"], item["name"].casefold(), item["region"].casefold()))
    return {"default_city_id": default_city(session).id, "items": items}


class CatalogAddress(BaseModel):
    catalog_url: str = Field(max_length=1000)


@router.put("/{city_id}/catalog")
def update_catalog(city_id: int, body: CatalogAddress, request: Request, property_kind: PropertyKind = "apartment",
                   session: Session = Depends(get_session)):
    city = editable_city(request, session, city_id)
    if session.scalar(select(CollectionRun.id).join(Crawl, CollectionRun.crawl_id == Crawl.id).where(
            Crawl.city_id == city_id, CollectionRun.status.in_(["queued", "running"])).limit(1)):
        raise HTTPException(409, "Адрес каталога можно изменить после остановки текущего сбора.")
    try:
        parts = urlsplit(body.catalog_url)
        match = re.fullmatch(rf"/([a-z0-9_-]+)/{CATEGORIES[property_kind]}/prodam(?:-[A-Za-z0-9]+)?/?", parts.path)
        valid = (parts.scheme == "https" and parts.hostname in {"avito.ru", "www.avito.ru"} and
                 not parts.username and not parts.password and parts.port in {None, 443} and match)
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(422, "Укажите HTTPS-адрес каталога продажи выбранного типа недвижимости на Авито.")
    city.slug = match[1]
    session.commit()
    return {"catalog_url": city_catalog(city, property_kind)}


class HouseRadius(BaseModel):
    radius_km: int | None = Field(default=None, ge=5, le=100)


@router.put('/{city_id}/house-radius')
def update_house_radius(city_id: int, body: HouseRadius, request: Request, session: Session = Depends(get_session)):
    city = editable_city(request, session, city_id)
    if session.scalar(select(CollectionRun.id).join(Crawl).where(Crawl.city_id == city_id,
            Crawl.property_kind == 'house', CollectionRun.status.in_(['queued', 'running'])).limit(1)):
        raise HTTPException(409, 'Остановите сбор домов перед изменением радиуса.')
    city.house_radius_km = body.radius_km
    session.commit()
    return {'radius_km': house_radius(city), 'custom': body.radius_km is not None}
