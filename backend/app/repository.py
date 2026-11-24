from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import Address, City, CollectionRun, Complex, FeatureValue, Listing, ListingCityScope, PriceObservation, utcnow
from app.parser import ListingData
from app.geography import distance_km
from app.listing_history import record_state
from app.map_index import locate_address

LISTING_FIELDS = (
    'avito_id', 'url', 'title', 'market_type_code', 'rooms', 'total_area', 'property_kind',
    'land_area', 'distance_to_center_km', 'source_distance_km', 'is_open_plan', 'kitchen_area',
    'living_area', 'floor', 'floors_total', 'ceiling_height', 'construction_year', 'source_updated_at',
)
OPTIONAL_REFRESH_FIELDS = {
    'complex_id', 'kitchen_area', 'living_area', 'floor', 'floors_total', 'ceiling_height',
    'construction_year', 'land_area', 'distance_to_center_km', 'source_distance_km',
}


class StaleObservation(ValueError):
    """A newer check has already been saved under the listing lock."""


def listing_features(session, features):
    pairs = list(dict.fromkeys((code, value) for code, values in features.items() for value in values))
    if not pairs:
        return []
    query = select(FeatureValue).where(tuple_(FeatureValue.type_code, FeatureValue.value).in_(pairs))
    known = {(row.type_code, row.value): row for row in session.scalars(query)}
    missing = sorted(set(pairs) - known.keys())
    if missing:
        session.execute(insert(FeatureValue).values([dict(type_code=code, value=value) for code, value in missing])
                        .on_conflict_do_nothing())
        known.update({(row.type_code, row.value): row for row in session.scalars(query)})
    return [known[pair] for pair in pairs]


def apply_fields(listing, data, values, merge_missing):
    for key, value in values.items():
        if merge_missing and value is None:
            if key in OPTIONAL_REFRESH_FIELDS:
                continue
            if key == 'rooms' and data.property_kind == 'house' and not data.is_open_plan:
                continue
        setattr(listing, key, value)
    if merge_missing and listing.floor is not None and listing.floors_total is not None and listing.floor > listing.floors_total:
        if data.floor is None:
            listing.floor = None
        elif data.floors_total is None:
            listing.floors_total = None


def distance_from(city, data):
    if None not in (city.latitude, city.longitude, data.latitude, data.longitude):
        return Decimal(str(distance_km((city.latitude, city.longitude), (data.latitude, data.longitude)))).quantize(Decimal('.01'))
    return None


def update_house_scopes(session, listing, city, data, observed_at):
    scope = next((scope for scope in listing.scopes if scope.city_id == city.id), None)
    if scope is None:
        scope = ListingCityScope(city_id=city.id, first_seen_at=observed_at)
        listing.scopes.append(scope)
    scope.distance_km = data.distance_to_center_km
    for scope in listing.scopes:
        distance = distance_from(session.get(City, scope.city_id), data)
        if distance is not None:
            scope.distance_km = distance


def lookup(session: Session, model, **keys):
    # ON CONFLICT also handles a simultaneous offline import without duplicate dictionaries.
    session.execute(insert(model).values(**keys).on_conflict_do_nothing())
    return session.scalar(select(model).filter_by(**keys))


def save_listing(session: Session, data: ListingData, run_id: int | None = None,
                 observed_at: datetime | None = None, city_id: int | None = None,
                 merge_missing: bool = False) -> tuple[Listing, bool]:
    observed_at = observed_at or utcnow()
    # Serializes observations for the same Avito ID, including the first INSERT.
    session.execute(select(func.pg_advisory_xact_lock(int(data.avito_id))))
    listing = session.scalar(select(Listing).where(Listing.avito_id == data.avito_id).with_for_update())
    is_new = listing is None
    if not is_new and observed_at < listing.last_seen_at:
        raise StaleObservation("Наблюдение старше уже сохранённой проверки объявления.")
    from app.cities import selected_city
    city = selected_city(session, city_id)
    owner = session.get(City, listing.address.city_id) if not is_new and data.property_kind == 'house' else city
    address = lookup(session, Address, city_id=owner.id, text=data.address)
    locate_address(session, address, owner, data.latitude, data.longitude)
    complex_row = lookup(session, Complex, city_id=city.id, name=data.complex_name) if data.complex_name else None
    values = {field: getattr(data, field) for field in LISTING_FIELDS}
    values.update(address_id=address.id, complex_id=complex_row.id if complex_row else None, last_seen_at=observed_at)
    if owner.id != city.id and data.latitude is not None:
        values['distance_to_center_km'] = distance_from(owner, data)
    if is_new:
        listing = Listing(**values, first_seen_at=observed_at)
        session.add(listing)
        session.flush()
    else:
        apply_fields(listing, data, values, merge_missing)

    features = {}
    if not is_new and merge_missing:
        for value in listing.features:
            features.setdefault(value.type_code, []).append(value.value)
    features.update(data.features)
    listing.features = listing_features(session, features)
    if data.property_kind == 'house':
        update_house_scopes(session, listing, city, data, observed_at)
    listing.is_archived, listing.archived_at, listing.archive_reason = False, None, None
    previous = session.scalar(select(PriceObservation).where(PriceObservation.listing_id == listing.id)
                              .order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc()).limit(1))
    changed = previous is not None and previous.amount != data.price
    if previous is None or changed:
        if previous and observed_at < previous.observed_at:
            raise StaleObservation("Нельзя добавить наблюдение раньше последней сохранённой цены.")
        session.add(PriceObservation(listing_id=listing.id, run_id=run_id, amount=data.price,
                                     observed_at=observed_at, source_updated_at=data.source_updated_at))
    session.flush()
    record_state(session, listing, observed_at)
    return listing, changed


def create_run(session: Session) -> CollectionRun:
    run = CollectionRun()
    session.add(run)
    session.flush()
    return run
