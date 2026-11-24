export const COLUMN_SETTINGS_KEY = 'housing-database-columns-v1';

// Stable keys keep display preferences independent of labels and column order.
export const TABLE_COLUMNS = [
  {key: 'market_type', label: 'Тип'},
  {key: 'offer_category', label: 'Предложение', width: 230},
  {key: 'listing_status', label: 'Актуальность'},
  {key: 'address', label: 'Адрес', width: 230},
  {key: 'district', label: 'Район'},
  {key: 'complex_name', label: 'Название ЖК'},
  {key: 'rooms', label: 'Количество комнат', sort: 'rooms'},
  {key: 'total_area', label: 'Общая площадь, м²', sort: 'total_area'},
  {key: 'kitchen_area', label: 'Площадь кухни, м²', sort: 'kitchen_area'},
  {key: 'living_area', label: 'Жилая площадь, м²', sort: 'living_area'},
  {key: 'floor', label: 'Этаж', sort: 'floor'},
  {key: 'house_type', label: 'Тип дома', feature: true},
  {key: 'construction_year', label: 'Год постройки'},
  {key: 'passenger_elevator', label: 'Пассажирский лифт', feature: true},
  {key: 'parking', label: 'Парковка', feature: true, width: 180},
  {key: 'balcony', label: 'Балкон или лоджия', feature: true},
  {key: 'extras', label: 'Дополнительно', feature: true, width: 180},
  {key: 'room_type', label: 'Тип комнат', feature: true},
  {key: 'ceiling_height', label: 'Высота потолков, м', sort: 'ceiling_height'},
  {key: 'bathroom', label: 'Санузел', feature: true},
  {key: 'windows', label: 'Окна', feature: true},
  {key: 'finish', label: 'Отделка', sort: 'finish', feature: true},
  {key: 'sale_method', label: 'Способ продажи', feature: true},
  {key: 'sale_conditions', label: 'Условия продажи', feature: true},
  {key: 'price', label: 'Цена', sort: 'price'},
  {key: 'price_per_sqm', label: 'Цена за м²', sort: 'price_per_sqm'},
  {key: 'source_updated_at', label: 'Дата обновления Авито'},
  {key: 'last_seen_at', label: 'Последняя проверка'},
  {key: 'url', label: 'Объявление'},
];

export const allColumnKeys = (kind = "apartment") => columnsFor(kind).map(column => column.key);

export function decodeColumnSettings(raw, kind = "apartment") {
  try {
    const selected = JSON.parse(raw);
    if (Array.isArray(selected)) {
      const known = allColumnKeys(kind).filter(key => selected.includes(key));
      const previousKeys = allColumnKeys(kind).filter(key => !['listing_status', 'offer_category', 'district'].includes(key));
      if (previousKeys.every(key => known.includes(key))) return allColumnKeys(kind);
      if (known.length) return known;
    }
  } catch { /* Invalid or obsolete preferences use the default columns. */ }
  return allColumnKeys(kind);
}

export function toggleColumn(selected, key, checked, kind = "apartment") {
  const next = allColumnKeys(kind).filter(item => item === key ? checked : selected.includes(item));
  return next.length ? next : selected;
}

export const HOUSE_FEATURES = {
  object_type: 'Вид объекта', readiness: 'Готовность', land_category: 'Категория земель',
  wall_material: 'Материал стен', terrace: 'Терраса или веранда', electricity: 'Электричество',
  heating: 'Отопление', water_supply: 'Водоснабжение', gas: 'Газ', sewerage: 'Канализация',
  internet_tv: 'Интернет и ТВ', communications: 'Коммуникации', recreation: 'Для отдыха',
  transport_access: 'Транспортная доступность', infrastructure: 'Инфраструктура',
};
export const HOUSE_COLUMNS = [
  {key:'object_type', label:'Вид объекта', feature:true}, {key:'readiness', label:'Готовность', feature:true},
  ...TABLE_COLUMNS.filter(column => ['offer_category','address','district','rooms','total_area'].includes(column.key))
    .map(column => column.key === 'total_area' ? {...column,label:'Площадь дома, м²'} : column),
  {key:'land_area', label:'Площадь участка, сот.', sort:'land_area'},
  {key:'distance_to_center_km', label:'От центра города, км', sort:'distance_to_center_km'},
  {key:'source_distance_km', label:'До центра по Авито, км'},
  {key:'floors_total', label:'Этажей в доме', sort:'floors_total'},
  ...TABLE_COLUMNS.filter(column => ['construction_year','parking','bathroom','finish','sale_conditions'].includes(column.key)),
  ...Object.entries(HOUSE_FEATURES).filter(([key]) => !['object_type','readiness','communications'].includes(key))
    .map(([key,label])=>({key,label,feature:true,width:180})),
  ...TABLE_COLUMNS.filter(column => ['price','price_per_sqm','source_updated_at','last_seen_at','url'].includes(column.key)),
];
export const columnsFor = kind => kind === 'house' ? HOUSE_COLUMNS : TABLE_COLUMNS;
export const columnSettingsKey = kind => kind === 'house' ? 'housing-house-columns-v1' : COLUMN_SETTINGS_KEY;
