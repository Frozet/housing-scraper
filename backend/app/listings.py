"""Filtering and sorting apply before pagination, across all saved listings."""
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func, literal, or_, select, true, union_all
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.orm import Session

from app.db import get_session
from app.cities import listing_city_condition
from app.models import Complex, FeatureValue, Listing, ListingFeature, ListingState, ListingStateFeature, PriceObservation, ListingCityScope, ListingStateScope
from app.property_types import PropertyKind, HOUSE_FEATURES
from app.listing_history import month_cutoff, states_at
from app.offer_classification import OFFER_FIELDS, district_expression, offer_expressions, offer_label

router = APIRouter(prefix="/api/listings")
Nonnegative = Annotated[Decimal, Field(ge=0)]
NonnegativeInt = Annotated[int, Field(ge=0)]
Room = NonnegativeInt | Literal["open_plan"]
RANGES = {
    "total_area": "Общая площадь", "floor": "Этаж квартиры", "floors_total": "Этажей в доме",
    "ceiling_height": "Высота потолков", "price": "Цена", "price_per_sqm": "Цена за м²",
    "construction_year": "Год постройки", "land_area": "Площадь участка",
    "distance_to_center_km": "Расстояние от центра",
}


class ListingFilters(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False)
    city_id: Annotated[int, Field(ge=1)] | None = None
    property_kind: PropertyKind = "apartment"
    market_type: Literal["all", "secondary", "new"] = "all"
    listing_status: Literal['active', 'archived', 'all'] = 'active'
    sale_scope: list[Literal['whole', 'share', 'unknown']] = Field(default_factory=list)
    premise_kind: list[Literal['residential', 'apartment', 'non_residential', 'unknown']] = Field(default_factory=list)
    construction_status: list[Literal['ready', 'building', 'unfinished', 'on_order', 'unknown']] = Field(default_factory=list)
    district: list[str] = Field(default_factory=list)
    rooms: list[Room] = Field(default_factory=list)
    complex_id: list[NonnegativeInt] = Field(default_factory=list)
    total_area_min: Nonnegative | None = None
    total_area_max: Nonnegative | None = None
    floor_min: int | None = None
    floor_max: int | None = None
    floors_total_min: NonnegativeInt | None = None
    floors_total_max: NonnegativeInt | None = None
    ceiling_height_min: Nonnegative | None = None
    ceiling_height_max: Nonnegative | None = None
    price_min: NonnegativeInt | None = None
    price_max: NonnegativeInt | None = None
    price_per_sqm_min: Nonnegative | None = None
    price_per_sqm_max: Nonnegative | None = None
    balcony: Literal["all", "present", "balcony", "loggia", "absent", "unknown"] = "all"
    bathroom: list[Literal["combined", "separate"]] = Field(default_factory=list)
    finish: list[str] = Field(default_factory=list)
    house_type: list[str] = Field(default_factory=list)
    passenger_elevator: list[str] = Field(default_factory=list)
    parking: list[str] = Field(default_factory=list)
    land_area_min: Nonnegative | None = None
    land_area_max: Nonnegative | None = None
    distance_to_center_km_min: Nonnegative | None = None
    distance_to_center_km_max: Nonnegative | None = None
    house_bathroom: list[str] = Field(default_factory=list)
    object_type: list[str] = Field(default_factory=list)
    readiness: list[str] = Field(default_factory=list)
    land_category: list[str] = Field(default_factory=list)
    wall_material: list[str] = Field(default_factory=list)
    terrace: list[str] = Field(default_factory=list)
    electricity: list[str] = Field(default_factory=list)
    heating: list[str] = Field(default_factory=list)
    water_supply: list[str] = Field(default_factory=list)
    gas: list[str] = Field(default_factory=list)
    sewerage: list[str] = Field(default_factory=list)
    internet_tv: list[str] = Field(default_factory=list)
    communications: list[str] = Field(default_factory=list)
    recreation: list[str] = Field(default_factory=list)
    transport_access: list[str] = Field(default_factory=list)
    infrastructure: list[str] = Field(default_factory=list)
    construction_year_min: Annotated[int, Field(ge=1000, le=2999)] | None = None
    construction_year_max: Annotated[int, Field(ge=1000, le=2999)] | None = None

    @model_validator(mode="after")
    def ordered_ranges(self):
        for field, label in RANGES.items():
            low, high = getattr(self, field + "_min"), getattr(self, field + "_max")
            if low is not None and high is not None and low > high:
                raise ValueError(f"{label}: значение «от» больше значения «до».")
        return self


class PeriodFilters(ListingFilters):
    month: str | None = Field(default=None, pattern=r'^\d{4}-(?:0[1-9]|1[0-2])$')

    @model_validator(mode='after')
    def valid_month(self):
        month_cutoff(self.month)
        return self


class ListingQuery(ListingFilters):
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=500)
    sort_by: Literal["last_seen_at", "rooms", "total_area", "kitchen_area", "living_area", "floor",
                     "ceiling_height", "finish", "price", "price_per_sqm", "land_area", "distance_to_center_km", "floors_total", "construction_year"] = "last_seen_at"
    sort_order: Literal["asc", "desc"] = "desc"


class ListingRow(BaseModel):
    id: int
    avito_id: str
    url: str
    title: str
    offer: dict[str, str]
    offer_category: str
    district: str | None
    market_type: str
    address: str
    complex_id: int | None
    complex_name: str | None
    rooms: int | None
    is_open_plan: bool
    total_area: float
    kitchen_area: float | None
    living_area: float | None
    floor: int | None
    floors_total: int | None
    ceiling_height: float | None
    features: dict[str, list[str]]
    price: int | None
    construction_year: int | None
    property_kind: PropertyKind
    land_area: float | None
    distance_to_center_km: float | None
    source_distance_km: float | None
    is_archived: bool
    archived_at: datetime | None
    archive_reason: str | None
    price_per_sqm: float | None
    price_count: int
    source_updated_at: datetime | None
    first_seen_at: datetime
    last_seen_at: datetime


class ListingsPage(BaseModel):
    items: list[ListingRow]
    total: int
    total_all: int
    page: int
    page_size: int
    sort_by: str
    sort_order: Literal["asc", "desc"]


def latest_price_query(listing, cutoff=None):
    listing_id = listing.id if listing is Listing else listing.listing_id
    conditions = [PriceObservation.listing_id == listing_id]
    if cutoff is not None:
        conditions.append(PriceObservation.observed_at < cutoff)
    return (select(PriceObservation.amount).where(*conditions).correlate(listing)
            .order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc()).limit(1))


def current_prices(listing=Listing, cutoff=None):
    latest = latest_price_query(listing, cutoff).scalar_subquery()
    # The same rounded value is used for display, filtering, sorting and analytics.
    return latest, func.round(latest / listing.total_area, 2)



def scope_distance(city_id, listing=Listing):
    if city_id is None:
        return listing.distance_to_center_km
    scope = ListingCityScope if listing is Listing else ListingStateScope
    parent = scope.listing_id if listing is Listing else scope.state_id
    value = select(scope.distance_km).where(parent == listing.id, scope.city_id == city_id).scalar_subquery()
    return func.coalesce(value, listing.distance_to_center_km)

def normalized_value():
    return func.replace(func.lower(func.trim(FeatureValue.value)), "ё", "е")


def has_feature(code, values=None, listing=Listing):
    link = ListingFeature if listing is Listing else ListingStateFeature
    parent_id = link.listing_id if listing is Listing else link.state_id
    query = (select(parent_id).join(FeatureValue, FeatureValue.id == link.value_id)
             .where(parent_id == listing.id, FeatureValue.type_code == code))
    if values is not None:
        query = query.where(normalized_value().in_(values))
    return query.exists()


@router.get("/filter-options")
def filter_options(query: Annotated[PeriodFilters, Query()], session: Session = Depends(get_session)):
    cutoff = month_cutoff(query.month)
    listing = ListingState if cutoff else Listing
    scope = [listing_city_condition(query.city_id, listing)] if query.city_id is not None else []
    scope.append(listing.property_kind == query.property_kind)
    if cutoff:
        scope.append(listing.id.in_(states_at(cutoff)))
    if query.listing_status != 'all':
        scope.append(listing.is_archived.is_(query.listing_status == 'archived'))
    # Reuse exactly the same historical snapshot for every option list.
    scope_ids = select(listing.id).where(*scope).cte("filter_scope").prefix_with("MATERIALIZED")
    scoped_ids = select(scope_ids.c.id)
    reduced_scope = listing.id.in_(scoped_ids)
    link = ListingFeature if listing is Listing else ListingStateFeature
    parent_id = link.listing_id if listing is Listing else link.state_id
    house_codes = (*HOUSE_FEATURES, "bathroom")
    codes = ("finish", "house_type", "passenger_elevator", "parking") + (house_codes if query.property_kind == 'house' else ())
    array = func.jsonb_build_array
    response = union_all(
        select(literal("complex"), array(Complex.id, Complex.name)).select_from(Complex)
        .join(listing, listing.complex_id == Complex.id).where(reduced_scope).distinct(),
        select(literal("feature"), array(FeatureValue.type_code, FeatureValue.value)).select_from(FeatureValue)
        .join(link, link.value_id == FeatureValue.id).where(
            FeatureValue.type_code.in_(codes), parent_id.in_(scoped_ids)).distinct(),
        select(literal("room"), array(listing.rooms, func.bool_or(listing.is_open_plan),
            func.bool_or(listing.complex_id.is_(None)))).select_from(listing)
        .where(reduced_scope).group_by(listing.rooms),
        select(literal("district"), array(district_expression(listing))).select_from(listing)
        .where(reduced_scope).distinct(),
    )
    complexes, districts, summaries, features = [], [], [], {}
    for kind, values in session.execute(response):
        if kind == "complex":
            complexes.append({"id": values[0], "name": values[1]})
        elif kind == "feature":
            features.setdefault(values[0], []).append(values[1])
        elif kind == "room":
            summaries.append(values)
        else:
            districts.append(values[0])

    def feature_options(code):
        return sorted(features.get(code, []), key=lambda value: (0, int(value)) if value.isdecimal() else (1, value.casefold()))

    counts = sorted(count for count, _, _ in summaries if count is not None)
    rooms = [{"value": str(count), "label": "Студия" if count == 0 else str(count)} for count in counts]
    if any(open_plan for _, open_plan, _ in summaries):
        rooms.append({"value": "open_plan", "label": "Свободная планировка"})
    return {"complexes": sorted(complexes, key=lambda row: (row["name"].casefold(), row["name"], row["id"])),
            'offer_filters': {code: [{'value': value, 'label': label} for value, label in labels.items()]
                              for code, (_, labels) in OFFER_FIELDS.items()},
            'districts': [{'value': value or '__unknown__', 'label': value or 'Район не указан'}
                          for value in sorted(districts, key=lambda value: (value is None, (value or '').casefold()))],
            "has_no_complex": any(no_complex for _, _, no_complex in summaries),
            "rooms": rooms, "finishes": feature_options("finish"),
            "house_types": feature_options("house_type"), "passenger_elevators": feature_options("passenger_elevator"),
            "parking_options": feature_options("parking"),
            "house_features": {code: feature_options(code) for code in house_codes}}


def filter_conditions(query: ListingFilters, latest, price_per_sqm, listing=Listing):
    columns = {key: getattr(listing, key) for key in ("total_area", "floor", "floors_total", "ceiling_height", "construction_year", "land_area", "distance_to_center_km")}
    columns.update(price=latest, price_per_sqm=price_per_sqm, distance_to_center_km=scope_distance(query.city_id, listing))
    conditions = [listing.property_kind == query.property_kind]
    if query.listing_status != "all":
        conditions.append(listing.is_archived.is_(query.listing_status == "archived"))
    if query.city_id is not None:
        conditions.append(listing_city_condition(query.city_id, listing))
    if query.market_type != "all":
        conditions.append(listing.market_type_code == query.market_type)
    if any(getattr(query, code) for code in OFFER_FIELDS):
        for code, expression in offer_expressions(listing).items():
            if values := getattr(query, code):
                conditions.append(expression.in_(values))
    if query.district:
        conditions.append(func.coalesce(district_expression(listing), '__unknown__').in_(query.district))
    if query.rooms:
        room_conditions = [listing.rooms.in_([value for value in query.rooms if isinstance(value, int)])]
        if "open_plan" in query.rooms:
            room_conditions.append(listing.is_open_plan.is_(True))
        conditions.append(or_(*room_conditions))
    if query.complex_id:
        complexes = [listing.complex_id.in_([value for value in query.complex_id if value != 0])]
        if 0 in query.complex_id:
            complexes.append(listing.complex_id.is_(None))
        conditions.append(or_(*complexes))
    for field in RANGES:
        low, high = getattr(query, field + "_min"), getattr(query, field + "_max")
        if low is not None:
            conditions.append(columns[field] >= low)
        if high is not None:
            conditions.append(columns[field] <= high)
    if query.balcony != "all":
        balcony_values = {
            "present": ["балкон", "лоджия"], "balcony": ["балкон"], "loggia": ["лоджия"],
            "absent": ["нет", "отсутствует", "отсутствуют", "без балкона", "нет балкона", "нет балкона и лоджии"],
        }
        conditions.append(~has_feature("balcony", listing=listing) if query.balcony == "unknown" else
                          has_feature("balcony", balcony_values[query.balcony], listing))
    if query.bathroom:
        conditions.append(has_feature("bathroom", ["совмещенный" if value == "combined" else "раздельный"
                                                   for value in query.bathroom], listing))
    for code in ("finish", "house_type", "passenger_elevator", "parking", *HOUSE_FEATURES):
        if values := getattr(query, code):
            conditions.append(has_feature(code, [value.strip().casefold().replace('ё', 'е') for value in values], listing))
    if query.house_bathroom:
        conditions.append(has_feature('bathroom', [value.strip().casefold().replace('ё', 'е') for value in query.house_bathroom], listing))
    return conditions


@router.get("", response_model=ListingsPage)
def listings(query: Annotated[ListingQuery, Query()], session: Session = Depends(get_session)):
    price_filtered = any(getattr(query, f'{field}_{bound}') is not None
                         for field in ('price', 'price_per_sqm') for bound in ('min', 'max'))
    price_lookup = None
    # For ordinary sorts PostgreSQL can fetch price only for the visible page.
    # Join it once when filtering by price uses it in multiple expressions.
    if price_filtered or query.sort_by == 'price_per_sqm':
        price_lookup = latest_price_query(Listing).lateral('latest_price')
        latest = price_lookup.c.amount
        price_per_sqm = func.round(latest / Listing.total_area, 2)
    else:
        latest, price_per_sqm = current_prices()
    price_count = select(func.count(PriceObservation.id)).where(PriceObservation.listing_id == Listing.id).scalar_subquery()
    finish = (select(func.string_agg(func.lower(FeatureValue.value),
                  aggregate_order_by(literal(", "), func.lower(FeatureValue.value))))
              .select_from(ListingFeature).join(FeatureValue, FeatureValue.id == ListingFeature.value_id)
              .where(ListingFeature.listing_id == Listing.id, FeatureValue.type_code == "finish").scalar_subquery())
    columns = {key: getattr(Listing, key) for key in (
        "last_seen_at", "rooms", "total_area", "kitchen_area", "living_area", "floor", "ceiling_height", "land_area", "distance_to_center_km", "floors_total", "construction_year")}
    distance = scope_distance(query.city_id)
    columns.update(finish=finish, price=latest, price_per_sqm=price_per_sqm, distance_to_center_km=distance)
    conditions = filter_conditions(query, latest, price_per_sqm)
    offers = offer_expressions()
    column = columns[query.sort_by]
    order = column.asc() if query.sort_order == "asc" else column.desc()
    statement = select(Listing, latest.label("price"), price_count.label("price_count"),
                       price_per_sqm.label("price_per_sqm"), distance.label("distance_km"),
                       *[value.label(code) for code, value in offers.items()],
                       district_expression().label('district')).select_from(Listing)
    if price_lookup is not None:
        statement = statement.outerjoin(price_lookup, true())
    rows = session.execute(statement.where(*conditions)
                           .order_by(order.nulls_last(), Listing.id.desc())
                           .offset((query.page - 1) * query.page_size).limit(query.page_size))
    items = []
    for listing, price, count, unit_price, distance_km, scope, premise, stage, district in rows:
        features = {}
        for value in listing.features:
            features.setdefault(value.type_code, []).append(value.value)
        if "finish" in features:
            features["finish"].sort(key=str.casefold)
        items.append({
            "id": listing.id, "avito_id": listing.avito_id, "url": listing.url, "title": listing.title,
            'offer': dict(sale_scope=scope, premise_kind=premise, construction_status=stage),
            'offer_category': offer_label(dict(sale_scope=scope, premise_kind=premise, construction_status=stage)),
            'district': district,
            "market_type": listing.market_type.name, "address": listing.address.text,
            "complex_id": listing.complex_id, "complex_name": listing.complex.name if listing.complex else None,
            "rooms": listing.rooms, "is_open_plan": listing.is_open_plan,
            "total_area": listing.total_area, "kitchen_area": listing.kitchen_area,
            "living_area": listing.living_area, "floor": listing.floor, "floors_total": listing.floors_total,
            "ceiling_height": listing.ceiling_height, "features": features, "price": price,
            "construction_year": listing.construction_year, "property_kind": listing.property_kind,
            "land_area": listing.land_area, "distance_to_center_km": distance_km,
            "source_distance_km": listing.source_distance_km,
            "is_archived": listing.is_archived, "archived_at": listing.archived_at, "archive_reason": listing.archive_reason,
            "price_per_sqm": unit_price, "price_count": count, "source_updated_at": listing.source_updated_at,
            "first_seen_at": listing.first_seen_at, "last_seen_at": listing.last_seen_at,
        })
    total_all = session.scalar(select(func.count(Listing.id)).where(Listing.property_kind == query.property_kind, *([listing_city_condition(query.city_id)] if query.city_id else [])))
    total = total_all
    if len(conditions) > (2 if query.city_id is not None else 1):
        count_query = select(func.count(Listing.id)).select_from(Listing).where(*conditions)
        if price_filtered:
            count_query = count_query.outerjoin(price_lookup, true())
        total = session.scalar(count_query)
    return {"items": items, "total": total, "total_all": total_all, "page": query.page,
            "page_size": query.page_size, "sort_by": query.sort_by, "sort_order": query.sort_order}
