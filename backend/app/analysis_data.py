"""A single SQL snapshot supplies distributions, comparisons, plots and duplicate candidates."""
from sqlalchemy import func, select

from app.listing_history import month_cutoff, states_at
from app.listings import current_prices, filter_conditions
from app.models import Address, City, Complex, FeatureValue, Listing, ListingFeature, ListingState, ListingStateFeature
from app.offer_classification import classify_offer, district_value


def candidate_rows(session, query):
    """Scan only grouping columns; detailed features are loaded for the selected page."""
    cutoff = month_cutoff(query.month)
    model = ListingState if cutoff else Listing
    identifier = model.listing_id if cutoff else model.id
    latest, unit_price = current_prices(model, cutoff)
    statement = (select(identifier.label('id'), model.property_kind, model.total_area,
        Address.text.label('address'), Address.map_key, Address.city_id.label('address_city_id'))
        .select_from(model).join(Address, Address.id == model.address_id)
        .where(*filter_conditions(query, latest, unit_price, model)).order_by(identifier))
    if cutoff:
        statement = statement.where(model.id.in_(states_at(cutoff)))
    return list(session.execute(statement).mappings()), cutoff


def load_analysis_rows(session, query, listing_ids=None):
    cutoff = month_cutoff(query.month)
    model = ListingState if cutoff else Listing
    listing_id = model.id if model is Listing else model.listing_id
    link = ListingFeature if model is Listing else ListingStateFeature
    parent = link.listing_id if model is Listing else link.state_id
    features = (select(func.jsonb_agg(func.jsonb_build_object('code', FeatureValue.type_code, 'value', FeatureValue.value)))
        .select_from(link).join(FeatureValue, FeatureValue.id == link.value_id).where(parent == model.id)
        .correlate(model).scalar_subquery())
    latest, unit_price = current_prices(model, cutoff)
    fields = ['url', 'title', 'property_kind', 'market_type_code', 'address_id', 'complex_id', 'rooms', 'is_open_plan',
              'total_area', 'land_area', 'floor', 'floors_total', 'construction_year', 'source_updated_at']
    statement = select(listing_id.label('id'), *[getattr(model, field) for field in fields],
        latest.label('price'), unit_price.label('price_per_sqm'), features.label('feature_rows'),
        (model.last_seen_at if model is Listing else model.observed_at).label('checked_at'),
        Address.text.label('address'), Address.map_key, Address.city_id.label('address_city_id'),
        City.name.label('city_name'), Complex.name.label('complex_name')).select_from(model)
    if cutoff:
        statement = statement.add_columns(Listing.avito_id).join(Listing, Listing.id == model.listing_id).where(model.id.in_(states_at(cutoff)))
    else:
        statement = statement.add_columns(model.avito_id)
    statement = (statement.join(Address, Address.id == model.address_id).join(City, City.id == Address.city_id)
        .outerjoin(Complex, Complex.id == model.complex_id).where(*filter_conditions(query, latest, unit_price, model)))
    if listing_ids is not None:
        statement = statement.where(listing_id.in_(listing_ids))
    result = []
    for record in session.execute(statement.order_by(listing_id)).mappings():
        row = dict(record)
        row['features'] = {}
        for item in row.pop('feature_rows') or []:
            row['features'].setdefault(item['code'], []).append(item['value'])
        row.update(classify_offer(row['title'], row['property_kind'], row['market_type_code'], row['features']))
        row['district'] = district_value(row['features'], row['address'])
        result.append(row)
    return result, cutoff
