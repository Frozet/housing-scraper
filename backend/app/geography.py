"""Conservative house matching: missing/ambiguous addresses never get a guessed pin."""
import math
import re
from decimal import Decimal, InvalidOperation


def coordinates(latitude, longitude):
    try:
        lat, lon = Decimal(str(latitude)), Decimal(str(longitude))
        if not lat.is_finite() or not lon.is_finite() or not (-85 <= lat <= 85 and -180 <= lon <= 180):
            return None
        return lat.quantize(Decimal('.0000001')), lon.quantize(Decimal('.0000001'))
    except (InvalidOperation, ValueError, TypeError):
        return None


def normalize(value):
    return re.sub(r'\s+', ' ', value.casefold().replace('ё', 'е')).strip()


def street_key(value):
    value = normalize(value)
    value = re.sub(r'\b(?:улица|ул|проспект|просп|пр-т|переулок|пер|бульвар|б-р|проезд|пр|набережная|наб)\b\.?', ' ', value)
    return re.sub(r'[^а-яa-z0-9]+', '', value)


def house_key(value):
    value = normalize(value).replace('«', '').replace('»', '')
    value = re.sub(r'\b(?:корпус|корп)\.?\s*', 'к', value)
    value = re.sub(r'\b(?:строение|стр)\.?\s*', 'с', value)
    value = re.sub(r'\s+', '', value)
    # Slash numbers, building suffixes and letters remain distinct.
    return value if re.fullmatch(r'\d+[а-яa-z]?(?:/\d+[а-яa-z]?)?(?:[кс]\d+[а-яa-z]?)?', value) else None


def building_key(address, city_name=''):
    value = normalize(address)
    if city_name:
        for part in value.split(','):
            locality = re.fullmatch(r'(?:(?:г|с|п|пос|рп|д)\.\s*|(?:город|село|поселок|деревня|рабочий поселок|г|с|п|пос|рп|д)\s+)([а-я][а-я0-9 -]*)', part.strip())
            if locality and locality[1] != normalize(city_name):
                return None
        value = re.sub(rf'^(?:(?:г\.?|город)\s*)?{re.escape(normalize(city_name))}\s*[,;]\s*', '', value)
    # A land plot or a named development is not a confirmed house number.
    if re.search(r'\b(?:з/у|участок|жк)\b', value):
        return None
    match = re.fullmatch(r'(.+?)(?:\s*,\s*(?:д(?:ом)?\.?\s*)?|\s+д(?:ом)?\.?\s*|\s+)(\d[^,;]*)', value)
    if not match:
        return None
    street, house = street_key(match[1].rsplit(',', 1)[-1]), house_key(match[2])
    return f'{street}|{house}' if street and house and '/' not in match[1] else None


def distance_km(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, [float(a[0]), float(a[1]), float(b[0]), float(b[1])])
    h = math.sin((lat2-lat1)/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
    return 12742 * math.asin(min(1, math.sqrt(h)))
