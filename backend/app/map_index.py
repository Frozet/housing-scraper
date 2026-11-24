"""One cached OpenStreetMap address import per city, without per-listing geocoding."""
from datetime import timedelta
import json
import logging
import math

import httpx
from sqlalchemy import delete, or_, select, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.db import engine
from app.geography import building_key, coordinates, distance_km, house_key, street_key
from app.models import Address, City, Listing, MapBuilding, MapImport, utcnow

log = logging.getLogger(__name__)
LOCK = 765031894475
MAX_MAP_BYTES = 25_000_000


def locate_address(session, address, city, latitude=None, longitude=None):
    address.map_key = building_key(address.text, city.name)
    if address.coordinate_source == 'manual':
        return
    if point := coordinates(latitude, longitude):
        address.latitude, address.longitude = point
        address.coordinate_source, address.geolocated_at = 'avito', utcnow()
    elif address.latitude is None or address.coordinate_source == 'osm':
        building = session.get(MapBuilding, (city.id, address.map_key)) if address.map_key else None
        if building:
            address.latitude, address.longitude = building.latitude, building.longitude
            address.coordinate_source, address.geolocated_at = 'osm', utcnow()
        elif address.coordinate_source == 'osm':
            address.latitude = address.longitude = address.coordinate_source = address.geolocated_at = None


def address_index(elements, city):
    candidates = {}
    for element in elements:
        tags = element.get('tags', {})
        house = house_key(tags.get('addr:housenumber', ''))
        street = street_key(tags.get('addr:street') or tags.get('addr:place') or '')
        center = element.get('center', element)
        point = coordinates(center.get('lat'), center.get('lon'))
        if not street or not house or not point:
            continue
        if city.latitude is not None and distance_km(point, (city.latitude, city.longitude)) > 120:
            continue
        candidates.setdefault(f'{street}|{house}', []).append(point)
    # Several entrances of one house are fine; distant homonyms are ambiguous.
    return {key: points[0] for key, points in candidates.items()
            if all(distance_km(points[0], point) < .15 for point in points)}


def fetch_buildings(city):
    if city.latitude is None or city.longitude is None:
        raise ValueError('Для города пока нет координат центра. Можно указать точки адресов вручную.')
    names = [city.name, 'город ' + city.name, 'городской округ ' + city.name,
             'городской округ город ' + city.name, 'городское поселение ' + city.name]
    areas = ''.join(f'area["boundary"="administrative"]["name"={json.dumps(name, ensure_ascii=False)}];' for name in names)
    lat, lon = float(city.latitude), float(city.longitude)
    delta = 1.1 / max(.1, math.cos(math.radians(lat)))
    bbox = f'{max(-85,lat-1.1)},{max(-180,lon-delta)},{min(85,lat+1.1)},{min(180,lon+delta)}'
    # Exact indexed names avoid an expensive nearby-relation/regex scan.
    # The spatial intersection also isolates homonymous cities in other regions.
    query = (f'[out:json][timeout:50][maxsize:134217728];({areas})->.city;'
             f'nwr(area.city)["addr:housenumber"]({bbox});out tags center;')
    with (httpx.Client(timeout=60, headers={'User-Agent': 'HousingScraperLocal/1.0 (city address map)'}) as client,
          client.stream('POST', settings().map_overpass_url, data={'data': query}) as response):
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes(chunk_size=65_536):
            if len(body) + len(chunk) > MAX_MAP_BYTES:
                raise ValueError('Справочник города слишком большой.')
            body.extend(chunk)
        data = json.loads(body)
    if not isinstance(data, dict) or data.get('remark') or not isinstance(data.get('elements'), list):
        raise ValueError('OpenStreetMap не закончил обработку запроса. Повторите подготовку карты позже.')
    return address_index(data['elements'], city)


def apply_index(session, city):
    addresses = session.scalars(select(Address).where(Address.city_id == city.id,
        Address.id.in_(select(Listing.address_id)))).all()
    for address in addresses:
        locate_address(session, address, city)


def map_failure(session, city_id, exc):
    job = session.get(MapImport, city_id)
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    temporary = isinstance(exc, (OperationalError, httpx.TransportError)) or status in {429, 502, 503, 504}
    now = utcnow()
    # A transient fault gets at most two retries; a bad response or invalid write stops.
    if temporary and job.attempts < 3:
        job.status, job.retry_at = 'queued', now + timedelta(seconds=30 * job.attempts)
    else:
        job.status, job.retry_at, job.finished_at = 'failed', None, now
    job.error = (f'Не удалось загрузить адреса OpenStreetMap ({status}).' if status else
                 'Не удалось подготовить адреса OpenStreetMap. Повторите позже или укажите точку вручную.')
    log.warning('map city=%s stage=import outcome=%s attempt=%s error=%s',
                city_id, job.status, job.attempts, type(exc).__name__)


def process_job():
    with engine.connect() as connection:
        if not connection.scalar(text('SELECT pg_try_advisory_lock(:key)'), {'key': LOCK}):
            return
        connection.commit()
        try:
            with Session(bind=connection, expire_on_commit=False) as session:
                # Holding the advisory lock proves any old "running" task was interrupted.
                job = session.scalar(select(MapImport).where(MapImport.status.in_(['queued', 'running']))
                                     .where(or_(MapImport.retry_at.is_(None), MapImport.retry_at <= utcnow()))
                                     .order_by(MapImport.requested_at).limit(1))
                if job is None:
                    return
                job.status, job.error = 'running', None
                job.attempts += 1
                job.retry_at = None
                city_id = job.city_id
                city = session.get(City, job.city_id)
                session.commit()
                try:
                    index = fetch_buildings(city)
                    session.execute(delete(MapBuilding).where(MapBuilding.city_id == city.id))
                    session.add_all(MapBuilding(city_id=city.id, key=key, latitude=point[0], longitude=point[1])
                                    for key, point in index.items())
                    session.flush()
                    apply_index(session, city)
                    job.status, job.building_count = 'completed', len(index)
                    job.finished_at = utcnow()
                    session.commit()
                except (SQLAlchemyError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                    session.rollback()
                    # Mark the outcome in a clean transaction after rolling back the index write.
                    with Session(engine) as recovery, recovery.begin():
                        map_failure(recovery, city_id, exc)
        finally:
            connection.execute(text('SELECT pg_advisory_unlock(:key)'), {'key': LOCK})
            connection.commit()


def worker(stop):
    while not stop.is_set():
        try:
            process_job()
        except Exception:
            log.exception('Map worker failed')
        stop.wait(2)
