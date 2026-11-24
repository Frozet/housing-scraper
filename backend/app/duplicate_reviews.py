"""Address + exact area produces candidates only, never automatic property identity."""
from collections import defaultdict
from decimal import Decimal
import hashlib
import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis_data import candidate_rows, load_analysis_rows
from app.db import get_session
from app.geography import building_key, normalize
from app.listings import PeriodFilters
from app.models import DuplicateReview, Listing, utcnow
from app.offer_classification import offer_label

router = APIRouter(prefix='/api/analytics/duplicates')


def candidate_key(row):
    if not building_key(row['address']):
        return None
    # Geocoding keys may parse a trailing section/entrance number as a building.
    # Preserve the full address in those cases so unrelated streets never collide.
    qualified = re.search(r'(?:^|[,\s])(?:секц(?:ия)?|корп(?:ус)?|стр(?:оение)?|кв(?:артира)?|пом(?:ещение)?|подъезд)\.?\s*\d', normalize(row['address']))
    address = normalize(row['address']) if qualified else row['map_key'] or normalize(row['address'])
    return row['address_city_id'], row['property_kind'], address, str(row['total_area'].quantize(Decimal('.01')))


def fingerprint(row):
    values = [candidate_key(row), row['rooms'], row['is_open_plan'], row['floor'], row['floors_total'], row['complex_id'],
              row['sale_scope'], row['premise_kind'], row['construction_status']]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False).encode()).hexdigest()


def candidate_groups(rows):
    groups = defaultdict(list)
    for row in rows:
        if key := candidate_key(row):
            groups[key].append(row)
    return [group for group in groups.values() if len(group) > 1]


class DuplicateQuery(PeriodFilters):
    duplicate_page: int = Field(default=1, ge=1)


@router.get('')
def duplicates(query: Annotated[DuplicateQuery, Query()], session: Session = Depends(get_session)):
    rows, cutoff = candidate_rows(session, query)
    groups = sorted(candidate_groups(rows), key=lambda group: (-len(group), group[0]['address'].casefold(), group[0]['total_area']))
    size = 5
    selected = groups[(query.duplicate_page - 1) * size:query.duplicate_page * size]
    ids = [row['id'] for group in selected for row in group]
    detailed = {row['id']: row for row in load_analysis_rows(session, query, listing_ids=ids)[0]} if ids else {}
    statement = select(DuplicateReview).where(DuplicateReview.left_id.in_(ids), DuplicateReview.right_id.in_(ids))
    if cutoff:
        statement = statement.where(DuplicateReview.reviewed_at < cutoff)
    statement = statement.distinct(DuplicateReview.left_id, DuplicateReview.right_id).order_by(
        DuplicateReview.left_id, DuplicateReview.right_id, DuplicateReview.reviewed_at.desc(), DuplicateReview.id.desc())
    reviews = session.scalars(statement).all() if ids else []
    result = []
    for group in selected:
        # A live update between queries must not attach a review to a different grouping.
        key = candidate_key(group[0])
        group = [detailed[row['id']] for row in group if row['id'] in detailed and candidate_key(detailed[row['id']]) == key]
        if len(group) < 2:
            continue
        members = {row['id']: row for row in group}
        checks = []
        for review in reviews:
            if review.left_id in members and review.right_id in members:
                stale = review.left_signature != fingerprint(members[review.left_id]) or review.right_signature != fingerprint(members[review.right_id])
                checks.append(dict(left_id=review.left_id, right_id=review.right_id, decision=review.decision,
                                   stale=stale, reviewed_at=review.reviewed_at))
        result.append({'key': hashlib.sha256(json.dumps(candidate_key(group[0]), ensure_ascii=False).encode()).hexdigest(),
            'address': group[0]['address'], 'total_area': group[0]['total_area'], 'reviews': checks,
            'members': [{**{key: row[key] for key in ('id', 'avito_id', 'url', 'title', 'rooms', 'floor', 'floors_total', 'price', 'complex_name')},
                         'offer_category': offer_label(row), 'signature': fingerprint(row)} for row in group]})
    return {'groups': result, 'total_groups': len(groups), 'candidate_count': sum(map(len, groups)),
            'page': query.duplicate_page, 'page_size': size, 'historical': cutoff is not None}


class ReviewInput(BaseModel):
    left_id: int = Field(ge=1)
    right_id: int = Field(ge=1)
    left_signature: str = Field(pattern=r'^[a-f0-9]{64}$')
    right_signature: str = Field(pattern=r'^[a-f0-9]{64}$')
    decision: Literal['same', 'different', 'pending']

    @model_validator(mode='after')
    def different_ids(self):
        if self.left_id == self.right_id:
            raise ValueError('Выберите два разных объявления.')
        return self


@router.post('/review')
def review_duplicates(request: Request, body: ReviewInput, session: Session = Depends(get_session)):
    if origin := request.headers.get('origin'):
        if urlsplit(origin).netloc != request.headers.get('host'):
            raise HTTPException(403, 'Проверка доступна только из интерфейса приложения.')
    first = session.get(Listing, body.left_id)
    if first is None:
        raise HTTPException(404, 'Объявление не найдено.')
    session.execute(select(Listing.id).where(Listing.id.in_([body.left_id, body.right_id])).order_by(Listing.id).with_for_update()).all()
    rows, _ = load_analysis_rows(session, PeriodFilters(property_kind=first.property_kind, listing_status='all'), [body.left_id, body.right_id])
    records = {row['id']: row for row in rows}
    if len(records) != 2 or candidate_key(rows[0]) is None or candidate_key(rows[0]) != candidate_key(rows[1]):
        raise HTTPException(409, 'Объявления больше не образуют пару по адресу и площади. Обновите список.')
    expected = {body.left_id: body.left_signature, body.right_id: body.right_signature}
    if any(fingerprint(row) != expected[row['id']] for row in rows):
        raise HTTPException(409, 'Характеристики объявлений изменились. Обновите список и проверьте пару заново.')
    left, right = sorted(records)
    values = dict(left_id=left, right_id=right, left_signature=expected[left], right_signature=expected[right],
                  decision=body.decision, reviewed_at=utcnow())
    session.add(DuplicateReview(**values))
    session.commit()
    return {'left_id': left, 'right_id': right, 'decision': body.decision, 'reviewed_at': values['reviewed_at']}
