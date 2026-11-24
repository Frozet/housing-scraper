"""Avito filters decoded from the apartment and house URLs supplied by the user."""
import base64
import json
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

PRICE_PREFIX = bytes.fromhex('01280102010201449203c6100145c69a0c')
HEADERS = {'apartment': bytes.fromhex('01280102010201449203c610'),
           'house': bytes.fromhex('01280102010201449403d010')}
PRICE_ID, RADIUS_ID = bytes.fromhex('c69a0c'), bytes.fromhex('e6fe0e')
MAX_PRICE = 2**63 - 1


def kind_from_url(url):
    return 'house' if '/doma_dachi_kottedzhi/' in urlsplit(url).path else 'apartment'


def encode_fields(kind, fields):
    payload = HEADERS[kind] + bytes([len(fields), 0x45])
    for key, value in fields:
        encoded = json.dumps(value, separators=(',', ':')).encode()
        payload += key + bytes([len(encoded)]) + encoded
    # Avito's alphabet is _ for + and ~ for / (not RFC 4648's - and _).
    return base64.b64encode(payload).decode().replace('+', '_').replace('/', '~').rstrip('=')


def decode_fields(url):
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get('f')
    if values is None:
        return {}
    try:
        if len(values) != 1:
            raise ValueError
        # Keep reading older URLs stored in the queue, whose encoder used - for +.
        raw = values[0].replace('_', '+').replace('-', '+').replace('~', '/')
        data = base64.b64decode(raw + '=' * (-len(raw) % 4), validate=True)
        prefix = HEADERS[kind_from_url(url)]
        if not data.startswith(prefix) or data[len(prefix) + 1] != 0x45:
            raise ValueError
        count, offset, fields = data[len(prefix)], len(prefix) + 2, {}
        for _ in range(count):
            key, length = data[offset:offset + 3], data[offset + 3]
            offset += 4
            if key not in {PRICE_ID, RADIUS_ID} or key in fields:
                raise ValueError
            value = json.loads(data[offset:offset + length])
            if not isinstance(value, dict) or set(value) != {'from', 'to'}:
                raise ValueError
            fields[key] = value
            offset += length
        if offset != len(data):
            raise ValueError
        if RADIUS_ID in fields:
            radius = fields[RADIUS_ID]
            if kind_from_url(url) != 'house' or radius['from'] is not None or type(radius['to']) is not int or not 5 <= radius['to'] <= 100:
                raise ValueError
        return fields
    except (ValueError, KeyError, IndexError, TypeError):
        raise ValueError('Не удалось подтвердить фильтр цены или радиуса Авито. Прогресс сохранён.') from None


def price_filter(minimum, maximum, *, property_kind='apartment', radius_km=None):
    if (type(minimum) is not int or not 0 <= minimum <= MAX_PRICE
            or maximum is not None and (type(maximum) is not int or not max(1, minimum) <= maximum <= MAX_PRICE)):
        raise ValueError('Некорректный диапазон цены.')
    fields = [(PRICE_ID, {'from': minimum, 'to': maximum or 0})]
    if radius_km is not None:
        fields.append((RADIUS_ID, {'from': None, 'to': int(radius_km)}))
    return encode_fields(property_kind, fields)


def price_bounds(url):
    bounds = decode_fields(url).get(PRICE_ID)
    if bounds is None:
        return None
    low, high = bounds['from'], bounds['to']
    if type(low) is not int or type(high) is not int:
        raise ValueError('Не удалось подтвердить фильтр цены Авито. Прогресс сохранён.')
    price_filter(low, high or None)
    return low, high or None


def radius_bounds(url):
    value = decode_fields(url).get(RADIUS_ID)
    return value['to'] if value else None


def canonical_filter_url(url):
    fields = decode_fields(url)
    if not fields:
        return url
    parts = urlsplit(url)
    encoded = encode_fields(kind_from_url(url), list(fields.items()))
    if parse_qs(parts.query)['f'] == [encoded]:
        return url
    query = [(key, encoded if key == 'f' else value)
             for key, value in parse_qsl(parts.query, keep_blank_values=True)]
    return urlunsplit(parts._replace(query=urlencode(query)))


def filter_label(url):
    bounds, radius = price_bounds(url), radius_bounds(url)
    price = 'без фильтра цены' if bounds is None else range_label(*bounds)
    return f'{price}; радиус {radius} км' if radius is not None else f'{price}; без фильтра радиуса'


def range_url(base, minimum, maximum, *, filtered=True):
    parts = urlsplit(base)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in {'p', 'f', 'context', 'localPriority'}]
    kind = kind_from_url(base)
    radius = radius_bounds(base) if kind == 'house' else None
    if filtered:
        query.append(('f', price_filter(minimum, maximum, property_kind=kind, radius_km=radius)))
    elif radius is not None:
        query.append(('f', encode_fields(kind, [(RADIUS_ID, {'from': None, 'to': radius})])))
    if filtered or radius is not None:
        city = parts.path.strip('/').split('/')[0]
        category, sale = ('doma_dachi_kottedzhi', 'ASgBAgICAUSUA9AQ') if kind == 'house' else ('kvartiry', 'ASgBAgICAUSSA8YQ')
        parts = parts._replace(path=f'/{city}/{category}/prodam-{sale}')
    query.append(('localPriority', '0'))
    return urlunsplit(parts._replace(query=urlencode(query), fragment=''))


def split_bounds(minimum, maximum):
    if (maximum is not None and minimum == maximum) or minimum == MAX_PRICE:
        return None
    midpoint = (minimum + maximum) // 2 if maximum is not None else min(MAX_PRICE - 1, max(20_000_000, minimum * 2) - 1)
    return (minimum, midpoint), (midpoint + 1, maximum)


def range_label(minimum, maximum):
    number = lambda value: f'{value:,}'.replace(',', ' ')
    return f'от {number(minimum)} ₽' if maximum is None else f'{number(minimum)}–{number(maximum)} ₽'
