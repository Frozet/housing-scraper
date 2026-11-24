"""Shared market definitions; apartments and houses keep independent queues."""
from typing import Literal

PropertyKind = Literal['apartment', 'house']
CATEGORIES = {'apartment': 'kvartiry', 'house': 'doma_dachi_kottedzhi'}
HOUSE_FEATURES = {
    'object_type': 'Вид объекта', 'readiness': 'Готовность', 'land_category': 'Категория земель',
    'wall_material': 'Материал стен', 'terrace': 'Терраса или веранда',
    'electricity': 'Электричество', 'heating': 'Отопление', 'water_supply': 'Водоснабжение',
    'gas': 'Газ', 'sewerage': 'Канализация', 'internet_tv': 'Интернет и ТВ',
    'communications': 'Коммуникации', 'recreation': 'Для отдыха',
    'transport_access': 'Транспортная доступность', 'infrastructure': 'Инфраструктура',
}


def house_radius(city):
    if city.house_radius_km is not None:
        return int(city.house_radius_km)
    # A search preference, not a geographic boundary: gentler than sqrt(population)
    # so large cities do not pull in an entire region. Irkutsk is the 35 km anchor.
    population = city.population
    return round(max(15, min(70, 35 * (population / 587225) ** (1 / 3)))) if population else 25
