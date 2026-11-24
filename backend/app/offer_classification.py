"""Independent offer attributes, based only on explicit listing information."""
import re

from sqlalchemy import case, func, literal, select

from app.models import Address, FeatureValue, Listing, ListingFeature, ListingStateFeature

OFFER_FIELDS = {
    'sale_scope': ('Объект продажи', {'whole': 'Целиком', 'share': 'Доля', 'unknown': 'Не указано'}),
    'premise_kind': ('Назначение', {'residential': 'Жильё', 'apartment': 'Апартаменты',
                                  'non_residential': 'Другое нежилое', 'unknown': 'Не указано'}),
    'construction_status': ('Стадия объекта', {'ready': 'Готовый', 'building': 'Строится',
        'unfinished': 'Недострой', 'on_order': 'Строительство на заказ', 'unknown': 'Не указано'}),
}
SHARE = r'\b(?:доля|доли|долю|часть\s+(?:дома|квартиры|коттеджа))\b'
WHOLE = r'\b(?:целиком|цельный|целый|весь|полностью)\b'
OBJECT = r'\b(?:квартир\w*|апартамент\w*|дом|дач[аи]|коттедж|таунхаус)\b'
APARTMENT = r'\bапартамент\w*'
RESIDENTIAL = r'\b(?:квартир\w*|дом|дач[аи]|коттедж|таунхаус)\b'
NON_RESIDENTIAL = r'\bнежил\w*'
STAGE_PATTERNS = [
    ('on_order', r'\b(?:на\s+заказ|под\s+заказ|строительство\s+на\s+участке\s+заказчика)\b'),
    ('unfinished', r'\b(?:недостро\w*|не\s+(?:завершен\w*|готов\w*|построен\w*))\b'),
    ('building', r'\b(?:строится|строящийся|строящееся|строящиеся|в\s+стадии\s+строительства)\b'),
    ('ready', r'\b(?:готов(?:ый|ая|ое|ы|о)?|построен(?:а|о|ы)?|сдан(?:а|о|ы)?)\b'),
]
DISTRICT_PATTERNS = [r'(?:^|,\s*)(?:р-н|район)\s+([^,;]+)',
                     r'(?:^|,\s*)([^,;]+?)\s+(?:район|р-н)(?:[,;]|$)']


def matches(pattern, value):
    return bool(re.search(pattern, str(value).casefold().replace('ё', 'е')))


def stage(value):
    return next((code for code, pattern in STAGE_PATTERNS if matches(pattern, value)), None)


def district_from_text(value):
    for pattern in DISTRICT_PATTERNS:
        if match := re.search(pattern, value, re.I):
            return match[1].strip()
    return None


def district_value(features, address):
    specified = [value.strip() for value in features.get('district', []) if value.strip()]
    return min(specified) if specified else district_from_text(address)


def explicit_offer_features(params, address_text=''):
    """Free descriptions are intentionally excluded: an owner's shares needn't be for sale."""
    result = {}
    scope = ' '.join(params.get(key, '') for key in ('объект продажи', 'продается', 'продаётся', 'доля')).strip()
    if scope:
        whole = matches(WHOLE, scope) or scope.casefold() in {'нет', 'не доля', 'без долей'}
        share = matches(SHARE, scope) or re.search(r'\d+\s*/\s*\d+', scope) or params.get('доля', '').casefold() == 'да'
        result['sale_scope'] = ['whole' if whole else 'share' if share else 'whole' if matches(OBJECT, scope) else 'unknown']
    premise = ' '.join(params.get(key, '') for key in ('тип недвижимости', 'вид недвижимости', 'тип помещения', 'апартаменты'))
    if premise and (matches(APARTMENT, premise) or matches(NON_RESIDENTIAL, premise) or matches(RESIDENTIAL, premise)):
        result['premise_kind'] = ['apartment' if matches(APARTMENT, premise) else 'non_residential'
                                 if matches(NON_RESIDENTIAL, premise) else 'residential']
    if params.get('апартаменты', '').casefold() in {'да', 'нет'}:
        result['premise_kind'] = ['apartment' if params['апартаменты'].casefold() == 'да' else 'residential']
    readiness = ' '.join(params.get(key, '') for key in ('готовность', 'готовность дома', 'статус строительства', 'срок сдачи', 'статус дома'))
    if code := stage(readiness):
        result['construction_status'] = [code]
    district = params.get('район') or params.get('район города')
    if district:
        district = re.sub(r'^(?:р-н|район)\s+|\s+(?:район|р-н)$', '', district, flags=re.I).strip()
    district = district or district_from_text(address_text)
    if district and district.casefold() not in {'не указан', 'не указано', 'нет', '—', '-'}:
        result['district'] = [district]
    return result


def classify_offer(title, property_kind, market_type, features):
    scope = 'share' if matches(SHARE, title) else 'whole' if matches(WHOLE, title) or matches(OBJECT, title) else 'unknown'
    premise = 'apartment' if matches(APARTMENT, title) else 'non_residential' if matches(NON_RESIDENTIAL, title) else 'residential' if matches(RESIDENTIAL, title) else 'unknown'
    status = stage(title + ' ' + ', '.join(features.get('readiness', [])))
    status = status or ('ready' if property_kind == 'apartment' and market_type == 'secondary' else 'unknown')
    return {code: min((value.strip() for value in features.get(code, []) if value.strip() in OFFER_FIELDS[code][1]), default=fallback)
            for code, fallback in [('sale_scope', scope), ('premise_kind', premise), ('construction_status', status)]}


def feature_text(code, listing=Listing):
    link = ListingFeature if listing is Listing else ListingStateFeature
    parent = link.listing_id if listing is Listing else link.state_id
    return (select(func.string_agg(FeatureValue.value, literal(', '))).select_from(link)
        .join(FeatureValue, FeatureValue.id == link.value_id)
        .where(parent == listing.id, FeatureValue.type_code == code).correlate(listing).scalar_subquery())


def feature_choice(code, listing, allowed=None):
    link = ListingFeature if listing is Listing else ListingStateFeature
    parent = link.listing_id if listing is Listing else link.state_id
    value = func.nullif(func.btrim(FeatureValue.value), '')
    query = (select(func.min(value.collate('C'))).select_from(link)
             .join(FeatureValue, FeatureValue.id == link.value_id)
             .where(parent == listing.id, FeatureValue.type_code == code))
    if allowed is not None:
        query = query.where(value.in_(allowed))
    return query.correlate(listing).scalar_subquery()


def sql_matches(value, pattern):
    return func.replace(func.lower(value), 'ё', 'е').op('~')(pattern.replace(r'\b', r'\y'))


def offer_expressions(listing=Listing):
    title = listing.title
    scope = case((sql_matches(title, SHARE), 'share'), (sql_matches(title, WHOLE), 'whole'),
                 (sql_matches(title, OBJECT), 'whole'), else_='unknown')
    premise = case((sql_matches(title, APARTMENT), 'apartment'),
                   (sql_matches(title, NON_RESIDENTIAL), 'non_residential'),
                   (sql_matches(title, RESIDENTIAL), 'residential'), else_='unknown')
    readiness = func.concat(title, ' ', feature_text('readiness', listing))
    status = case(*[(sql_matches(readiness, pattern), code) for code, pattern in STAGE_PATTERNS],
                  ((listing.property_kind == 'apartment') & (listing.market_type_code == 'secondary'), 'ready'),
                  else_='unknown')
    return {code: func.coalesce(feature_choice(code, listing, tuple(OFFER_FIELDS[code][1])), fallback)
            for code, fallback in [('sale_scope', scope), ('premise_kind', premise), ('construction_status', status)]}


def district_expression(listing=Listing):
    address = select(Address.text).where(Address.id == listing.address_id).correlate(listing).scalar_subquery()
    return func.nullif(func.btrim(func.coalesce(feature_choice('district', listing),
                         *[func.substring(address, '(?i)' + pattern) for pattern in DISTRICT_PATTERNS])), '')


def offer_label(values):
    return ' · '.join(labels.get(values[code], 'Не указано') for code, (_, labels) in OFFER_FIELDS.items())
