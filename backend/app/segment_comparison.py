"""Side-by-side segments from the same filtered, dated listing snapshot."""
from collections import defaultdict
from decimal import Decimal

from app.distributions import percentile, rounded
from app.offer_classification import offer_label

DIMENSIONS = {
    'rooms': 'Комнаты', 'district': 'Районы', 'complex': 'Жилые комплексы', 'area': 'Площадь',
    'finish': 'Отделка', 'building_age': 'Возраст здания', 'building_type': 'Тип здания',
    'market_type': 'Первичный / вторичный рынок', 'offer': 'Категории предложений',
}


def segment_keys(row, dimension, reference):
    if dimension == 'rooms':
        if row['is_open_plan']:
            return [('open_plan', 'Свободная планировка', 100)]
        count = row['rooms']
        return [(str(count), 'Студии' if count == 0 else f'{count}-комнатные', count)] if count is not None else [('unknown', 'Комнаты не указаны', 999)]
    if dimension == 'district':
        value = row['district']
        return [(value or 'unknown', value or 'Район не указан', 0 if value else 999)]
    if dimension == 'complex':
        return [(str(row['complex_id']), row['complex_name'], 0)] if row['complex_id'] else [('unknown', 'ЖК не указан', 999)]
    if dimension == 'area':
        bounds = [0, 50, 100, 150, 200, 300] if row['property_kind'] == 'house' else [0, 30, 40, 60, 80, 100, 150]
        index = max(index for index, value in enumerate(bounds) if row['total_area'] >= value)
        label = f'от {bounds[index]} м²' if index == len(bounds) - 1 else f'до {bounds[index + 1]} м²' if index == 0 else f'{bounds[index]}–<{bounds[index + 1]} м²'
        return [(str(index), label, index)]
    if dimension in {'finish', 'building_type'}:
        code = 'finish' if dimension == 'finish' else 'wall_material' if row['property_kind'] == 'house' else 'house_type'
        values = row['features'].get(code, [])
        return [(value.casefold().replace('ё', 'е'), value, 0) for value in values] or [('unknown', 'Не указано', 999)]
    if dimension == 'building_age':
        year = row['construction_year']
        if year is None:
            return [('unknown', 'Год постройки не указан', 999)]
        age = reference.year - year
        if age < 0:
            return [('future', 'Год постройки позже периода', 998)]
        for index, (low, high) in enumerate([(0, 5), (6, 10), (11, 20), (21, 40), (41, 60)]):
            if low <= age <= high:
                return [(str(index), f'{low}–{high} лет', index)]
        return [('old', 'Более 60 лет', 5)]
    if dimension == 'offer':
        return [('|'.join(row[key] for key in ('sale_scope', 'premise_kind', 'construction_status')), offer_label(row), 0)]
    code = row['market_type_code']
    return [(code, {'new': 'Новостройки', 'secondary': 'Вторичка'}.get(code, 'Рынок не указан'), {'new': 0, 'secondary': 1}.get(code, 999))]


def comparison(rows, dimension, reference):
    groups, labels = defaultdict(dict), {}
    overlapping = False
    for row in rows:
        keys = segment_keys(row, dimension, reference)
        overlapping |= len(keys) > 1
        for key, label, order in keys:
            groups[key][row['id']] = row
            labels[key] = (order, label)
    result = []
    for key, group in sorted(groups.items(), key=lambda item: (labels[item[0]][0], labels[item[0]][1].casefold())):
        records = list(group.values())
        prices = sorted(Decimal(row['price']) for row in records if row['price'] is not None)
        units = sorted(Decimal(row['price_per_sqm']) for row in records if row['price_per_sqm'] is not None)
        ages = sorted(Decimal(int((reference - row['checked_at']).total_seconds() // 86400)) for row in records
                      if row['checked_at'] is not None and row['checked_at'] <= reference)
        result.append({'key': key, 'label': labels[key][1], 'count': len(records), 'priced_count': len(prices),
            'median_price': rounded(percentile(prices, '.5')), 'median_price_per_sqm': rounded(percentile(units, '.5')),
            'p25_price': rounded(percentile(prices, '.25')), 'p75_price': rounded(percentile(prices, '.75')),
            'p25_price_per_sqm': rounded(percentile(units, '.25')), 'p75_price_per_sqm': rounded(percentile(units, '.75')),
            'freshness': {'known_count': len(ages), 'median_days': rounded(percentile(ages, '.5')),
                          'within_30_days': sum(age <= 30 for age in ages),
                          'recent_share': rounded(Decimal(sum(age <= 30 for age in ages)) * 100 / len(records))}})
    return {'dimension': dimension, 'label': DIMENSIONS[dimension], 'rows': result, 'overlapping': overlapping}
