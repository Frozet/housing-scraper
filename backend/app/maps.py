from datetime import timedelta
from decimal import Decimal
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, any_, bindparam, func, select
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Session

from app.browser_jobs import collection_data_version
from app.cities import listing_city_condition, selected_city
from app.property_types import PropertyKind, house_radius
from app.config import settings
from app.db import get_session
from app.geography import building_key, distance_km
from app.listings import ListingFilters, current_prices, filter_conditions
from app.models import Address, Listing, MapImport, utcnow

router = APIRouter(prefix='/api/map')
LOCK_PREPARE = 765031894476


def local_change(request):
    origin = request.headers.get('origin')
    if origin and urlsplit(origin).netloc != request.headers.get('host'):
        raise HTTPException(403, 'Изменение разрешено только из интерфейса приложения.')


def job_status(session, city_id):
    job = session.get(MapImport, city_id)
    return {'status': job.status, 'error': job.error, 'building_count': job.building_count,
            'finished_at': job.finished_at} if job else None


@router.get('/status')
def map_status(city_id: Annotated[int, Query(ge=1)], property_kind: PropertyKind = 'apartment',
               session: Session = Depends(get_session)):
    return {"import": job_status(session, city_id),
            "data_version": collection_data_version(session, city_id, property_kind)}


@router.post('/prepare', status_code=202)
def prepare(request: Request, city_id: Annotated[int, Query(ge=1)], session: Session = Depends(get_session)):
    local_change(request)
    city = selected_city(session, city_id)
    session.execute(select(func.pg_advisory_xact_lock(LOCK_PREPARE)))
    job = session.get(MapImport, city.id)
    if job is None:
        session.add(MapImport(city_id=city.id))
    elif job.status == 'failed':
        if job.finished_at and job.finished_at > utcnow() - timedelta(minutes=5):
            raise HTTPException(409, 'Подготовку карты можно повторить через 5 минут после ошибки.')
        job.status, job.error, job.requested_at, job.finished_at = 'queued', None, utcnow(), None
        job.attempts, job.retry_at = 0, None
    session.commit()
    return job_status(session, city.id)


@router.get('')
def points(query: Annotated[ListingFilters, Query()], session: Session = Depends(get_session)):
    city = selected_city(session, query.city_id)
    version = collection_data_version(session, city.id, query.property_kind)
    imported = job_status(session, city.id)
    query = query.model_copy(update={'city_id': city.id})
    latest, unit = current_prices()
    rows = session.execute(select(Address.id, Address.text, Address.map_key, Address.latitude, Address.longitude,
                                  latest.label('price'), Address.coordinate_source)
        .join(Listing, Listing.address_id == Address.id).where(*filter_conditions(query, latest, unit))).all()
    groups, missing = {}, {}
    located = 0
    for row in rows:
        if row.latitude is None or row.longitude is None:
            item = missing.setdefault(row.id, {'id': row.id, 'address': row.text, 'count': 0})
            item['count'] += 1
            continue
        located += 1
        key = row.map_key or building_key(row.text, city.name) or str(row.id)
        group = groups.get(key)
        if group and distance_km((row.latitude, row.longitude), (group['latitude'], group['longitude'])) > .15:
            key += ':' + str(row.id)
        group = groups.setdefault(key, {'id': row.id, 'address': row.text, 'address_ids': set(),
            'latitude': float(row.latitude), 'longitude': float(row.longitude), 'count': 0,
            'price_min': None, 'price_max': None, 'price_sum': 0, 'priced_count': 0})
        group['address_ids'].add(row.id)
        group['count'] += 1
        if row.price is not None:
            group['price_sum'] += row.price
            group['priced_count'] += 1
            group['price_min'] = row.price if group['price_min'] is None else min(group['price_min'], row.price)
            group['price_max'] = row.price if group['price_max'] is None else max(group['price_max'], row.price)
    items = [{**{key: value for key, value in group.items() if key != 'price_sum'},
              'address_ids': sorted(group['address_ids']),
              'price_mean': group['price_sum'] / group['priced_count'] if group['priced_count'] else None}
             for group in groups.values()]
    config = settings()
    return {'city': {'id': city.id, 'name': city.name, 'radius_km': house_radius(city) if query.property_kind == 'house' else None,
                    'center': [float(city.latitude), float(city.longitude)] if city.latitude is not None else [61, 90]},
            'points': sorted(items, key=lambda item: item['id']), 'total': len(rows), 'located': located,
            'missing': sorted(missing.values(), key=lambda item: (-item['count'], item['address'])),
            'import': imported, 'data_version': version,
            'tiles': {'url': config.map_tile_url, 'attribution': config.map_tile_attribution}}


class MapListings(ListingFilters):
    address_id: list[Annotated[int, Field(ge=1)]] = Field(min_length=1, max_length=500)
    page: int = Field(default=1, ge=1)


@router.get('/listings')
def map_listings(query: Annotated[MapListings, Query()], session: Session = Depends(get_session)):
    return listings_at_addresses(session, query)


class MapGroupListings(MapListings):
    address_id: list[Annotated[int, Field(ge=1, le=2**31 - 1)]] = Field(min_length=1, max_length=100_000)


@router.post('/listings')
def group_listings(query: MapGroupListings, session: Session = Depends(get_session)):
    return listings_at_addresses(session, query)


def listings_at_addresses(session, query):
    city = selected_city(session, query.city_id)
    query = query.model_copy(update={'city_id': city.id})
    latest, unit = current_prices()
    address_ids = bindparam('map_address_ids', query.address_id, type_=ARRAY(Integer))
    conditions = filter_conditions(query, latest, unit) + [Listing.address_id == any_(address_ids)]
    rows = session.execute(select(Listing.id, Listing.avito_id, Listing.url, Listing.title,
            Address.text.label('address'), Listing.rooms, Listing.total_area, Listing.floor,
            Listing.floors_total, latest.label('price'))
        .join(Address, Address.id == Listing.address_id).where(*conditions)
        .order_by(latest.asc().nulls_last(), Listing.id).offset((query.page-1)*50).limit(50)).mappings().all()
    return {'items': [dict(row) for row in rows], 'page': query.page,
            'total': session.scalar(select(func.count(Listing.id)).where(*conditions)), 'page_size': 50}


class Point(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    latitude: Decimal = Field(ge=-85, le=85)
    longitude: Decimal = Field(ge=-180, le=180)


@router.put('/addresses/{address_id}')
def set_point(address_id: int, body: Point, request: Request,
              city_id: Annotated[int, Query(ge=1)], session: Session = Depends(get_session)):
    local_change(request)
    city = selected_city(session, city_id)
    address = session.get(Address, address_id)
    if address is None or (address.city_id != city.id and not session.scalar(select(
            select(Listing.id).where(Listing.address_id == address_id, Listing.property_kind == 'house',
                                     listing_city_condition(city.id)).exists()))):
        raise HTTPException(404, 'Адрес выбранного города не найден.')
    if city.latitude is not None and distance_km((body.latitude, body.longitude), (city.latitude, city.longitude)) > 120:
        raise HTTPException(422, 'Точка находится слишком далеко от выбранного города.')
    owner = city if address.city_id == city.id else selected_city(session, address.city_id)
    address.map_key = building_key(address.text, owner.name)
    address.latitude, address.longitude = body.latitude, body.longitude
    address.coordinate_source, address.geolocated_at = 'manual', utcnow()
    session.commit()
    return {'ok': True}
