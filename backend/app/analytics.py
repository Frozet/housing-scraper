"""Statistics of the current state: one observation per listing, no pagination."""
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from pydantic import Field, model_validator

from app.db import get_session
from app.listings import PeriodFilters
from app.listing_history import LOCAL_TIME, available_months
from app.analysis_data import load_analysis_rows
from app.distributions import budget_share, distribution_ordered, sorted_values
from app.duplicate_reviews import candidate_groups
from app.offer_classification import OFFER_FIELDS
from app.property_types import PropertyKind
from app.segment_comparison import comparison

router = APIRouter(prefix="/api/analytics")


@router.get('/months')
def analytics_months(city_id: Annotated[int | None, Query(ge=1)] = None, property_kind: PropertyKind = 'apartment',
                     session: Session = Depends(get_session)):
    return {'months': available_months(session, city_id, property_kind)}


def summarize(values):
    """Keep all tied modes; a sample without repeats has no mode (except a singleton)."""
    return summarize_ordered(sorted_values(values))


def summarize_ordered(ordered):
    count = len(ordered)
    if not count:
        return {"count": 0, "mean": None, "median": None, "modes": [], "mode_frequency": 0}
    frequencies = Counter(ordered)
    frequency = max(frequencies.values())
    middle = count // 2
    median = ordered[middle] if count % 2 else (ordered[middle - 1] + ordered[middle]) / 2
    rounded = lambda value: value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    modes = [value for value, repeats in frequencies.items() if repeats == frequency] if frequency > 1 or count == 1 else []
    return {"count": count, "mean": rounded(sum(ordered) / count), "median": rounded(median),
            "modes": modes, "mode_frequency": frequency if modes else 0}


class AnalyticsQuery(PeriodFilters):
    budget_min: int | None = Field(default=None, ge=0)
    budget_max: int | None = Field(default=None, ge=0)
    compare_by: Literal['rooms', 'district', 'complex', 'area', 'finish', 'building_age', 'building_type', 'market_type', 'offer'] = 'rooms'

    @model_validator(mode='after')
    def ordered_budget(self):
        if self.budget_min is not None and self.budget_max is not None and self.budget_min > self.budget_max:
            raise ValueError('Бюджет: значение «от» больше значения «до».')
        return self


@router.get("")
def analytics(query: Annotated[AnalyticsQuery, Query()], session: Session = Depends(get_session)):
    calculated_at = datetime.now(timezone.utc)
    rows, cutoff = load_analysis_rows(session, query)
    fields = ['price', 'price_per_sqm', 'total_area'] + (['land_area'] if query.property_kind == 'house' else [])
    points = [{key: row[key] for key in ['id', 'avito_id', *fields]} for row in rows if row['price'] is not None]
    groups = candidate_groups(rows)
    values = {field: sorted_values(row[field] for row in rows) for field in fields}
    offer_counts = {code: Counter() for code in OFFER_FIELDS}
    for row in rows:
        for code, counters in offer_counts.items():
            counters[row[code]] += 1
    return {"total": len(rows), "priced_count": len(points), "calculated_at": calculated_at,
            'period': {'month': query.month, 'as_of': cutoff,
                       'complete': query.month != datetime.now(LOCAL_TIME).strftime('%Y-%m')} if cutoff else None,
            'metrics': {field: summarize_ordered(series) for field, series in values.items()}, 'points': points,
            'distributions': {field: distribution_ordered(series) for field, series in values.items()},
            'budget': budget_share(values['price'], query.budget_min, query.budget_max),
            'comparison': comparison(rows, query.compare_by, ((cutoff - timedelta(microseconds=1)) if cutoff else calculated_at).astimezone(LOCAL_TIME)),
            'quality': {'offers': {code: [{'value': value, 'label': label, 'count': offer_counts[code][value]}
                                        for value, label in labels.items()]
                                   for code, (_, labels) in OFFER_FIELDS.items()},
                        'district_known': sum(row['district'] is not None for row in rows),
                        'duplicate_groups': len(groups), 'duplicate_candidates': sum(map(len, groups))}}
