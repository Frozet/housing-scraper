"""Temporal snapshots of observed listing attributes, separate from price changes."""
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, column, func, or_, select, text

from app.models import Listing, ListingState, ListingStateScope, utcnow

STATE_FIELDS = (
    'url', 'title', 'market_type_code', 'address_id', 'complex_id', 'rooms', 'is_open_plan',
    'total_area', 'kitchen_area', 'living_area', 'floor', 'floors_total', 'ceiling_height',
    'property_kind', 'land_area', 'distance_to_center_km', 'source_distance_km',
    'construction_year', 'source_updated_at', 'is_archived', 'archived_at', 'archive_reason',
)
LOCAL_TIME = ZoneInfo('Asia/Irkutsk')


def record_state(session, listing, observed_at):
    session.flush()
    previous = session.scalar(select(ListingState).where(ListingState.listing_id == listing.id)
        .order_by(ListingState.observed_at.desc(), ListingState.id.desc()).limit(1))
    values = {field: getattr(listing, field) for field in STATE_FIELDS}
    if previous and all(getattr(previous, field) == value for field, value in values.items()) and (
            {value.id for value in previous.features} == {value.id for value in listing.features}) and (
            {(scope.city_id, scope.distance_km) for scope in previous.scopes} == {(scope.city_id, scope.distance_km) for scope in listing.scopes}):
        return previous
    if previous and observed_at < previous.observed_at:
        raise ValueError('Нельзя записать состояние раньше последнего наблюдения объявления.')
    state = ListingState(listing_id=listing.id, observed_at=observed_at, **values)
    state.features = list(listing.features)
    state.scopes = [ListingStateScope(city_id=scope.city_id, distance_km=scope.distance_km) for scope in listing.scopes]
    session.add(state)
    session.flush()
    return state


def archive_listing(session, avito_id, reason, observed_at=None):
    # Use the same lock as save_listing to serialize status and price observations.
    from sqlalchemy import func
    session.execute(select(func.pg_advisory_xact_lock(int(avito_id))))
    listing = session.scalar(select(Listing).where(Listing.avito_id == avito_id).with_for_update())
    if listing is None or listing.is_archived:
        return False
    observed_at = observed_at or utcnow()
    if observed_at < listing.last_seen_at:
        return False
    listing.is_archived, listing.archived_at, listing.archive_reason = True, observed_at, reason
    listing.last_seen_at = observed_at
    record_state(session, listing, observed_at)
    return True


def month_cutoff(month):
    """Exclusive local month end; no future-state reconstruction."""
    if not month:
        return None
    try:
        year, number = map(int, month.split('-'))
        start = datetime(year, number, 1, tzinfo=LOCAL_TIME)
        now = datetime.now(LOCAL_TIME)
        if start > now:
            raise ValueError
        end = datetime(year + (number == 12), 1 if number == 12 else number + 1, 1, tzinfo=LOCAL_TIME)
    except (ValueError, OverflowError):
        raise ValueError('Выберите существующий месяц не позже текущего.') from None
    return min(end, now)


def states_at(cutoff):
    return (select(ListingState.id).where(ListingState.observed_at < cutoff)
            .distinct(ListingState.listing_id)
            .order_by(ListingState.listing_id, ListingState.observed_at.desc(), ListingState.id.desc()))


def available_months(session, city_id, property_kind, now=None):
    """Months in which the city and property kind have at least one as-of listing."""
    from app.cities import listing_city_condition

    now = (now or datetime.now(LOCAL_TIME)).astimezone(LOCAL_TIME)
    scope = [ListingState.property_kind == property_kind]
    if city_id is not None:
        scope.append(listing_city_condition(city_id, ListingState))

    first = select(func.min(ListingState.observed_at)).where(*scope).scalar_subquery()
    first_month = func.date_trunc('month', func.timezone(LOCAL_TIME.key, first))
    current_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    one_month = text("interval '1 month'")
    calendar = func.generate_series(first_month, current_month, one_month).table_valued(
        column('month', DateTime())).render_derived(name='calendar')
    cutoff = func.least(func.timezone(LOCAL_TIME.key, calendar.c.month + one_month), now)

    # Find the following state before filtering by scope: it can move the listing out of this city.
    history = select(ListingState.id, func.lead(ListingState.observed_at).over(
        partition_by=ListingState.listing_id,
        order_by=(ListingState.observed_at, ListingState.id)).label('next_at')).subquery()
    has_snapshot = (select(1).select_from(history).join(ListingState, ListingState.id == history.c.id)
                    .where(*scope, ListingState.observed_at < cutoff,
                           or_(history.c.next_at.is_(None), history.c.next_at >= cutoff))
                    .correlate(calendar).exists())
    return [month.strftime('%Y-%m') for month in session.scalars(
        select(calendar.c.month).where(has_snapshot).order_by(calendar.c.month.desc()))]
