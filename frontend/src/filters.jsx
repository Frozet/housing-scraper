import React, { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { HOUSE_FEATURES } from './table-columns.js';

const ranges = [
  {field: 'total_area', label: 'Общая площадь, м²', step: '0.01', min: 0},
  {field: 'floor', label: 'Этаж квартиры', step: '1'},
  {field: 'floors_total', label: 'Этажей в доме', step: '1', min: 0},
  {field: 'ceiling_height', label: 'Высота потолков, м', step: '0.01', min: 0},
  {field: 'price', label: 'Цена, ₽', step: '1', min: 0},
  {field: 'price_per_sqm', label: 'Цена за м², ₽', step: '0.01', min: 0},
  {field: 'land_area', label: 'Площадь участка, сот.', step: '0.01', min: 0},
  {field: 'distance_to_center_km', label: 'Расстояние от центра, км', step: '0.1', min: 0},
  {field: 'construction_year', label: 'Год постройки', step: '1', min: 1000, max: 2999},
];
const rangeByField = Object.fromEntries(ranges.map(definition => [definition.field, definition]));
const areaFields = {
  apartment: ['total_area', 'floor', 'floors_total'],
  house: ['total_area', 'floors_total', 'land_area', 'distance_to_center_km'],
};
const asChoices = values => (values || []).map(value => ({value, label: value}));

function complexChoices(options, mapId) {
  const choices = (options?.complexes || []).map(({id, name}) => ({value: mapId(id), label: name}));
  return options?.has_no_complex ? [{value: mapId(0), label: 'Без указания ЖК'}, ...choices] : choices;
}

export function emptyFilters() {
  return {complex_id: [], market_type: 'all', listing_status: 'active', month: '', rooms: [], balcony: 'all', bathroom: [], finish: [],
    sale_scope: [], premise_kind: [], construction_status: [], district: [],
    house_type: [], passenger_elevator: [], parking: [], house_bathroom: [],
    ...Object.fromEntries(Object.keys(HOUSE_FEATURES).map(key=>[key,[]])),
    ...Object.fromEntries(ranges.flatMap(({field}) => [[`${field}_min`, ''], [`${field}_max`, '']]))};
}

function Choices({legend, name, options, values, onChange}) {
  return <fieldset className="filter-field"><legend>{legend}</legend><div className="filter-choices">
    {options.map(({value, label}) => <label key={value}>
      <input type="checkbox" name={name} value={value} checked={values.includes(value)}
        onChange={e => onChange(e.target.checked ? [...values, value] : values.filter(item => item !== value))} />
      {label}
    </label>)}
    {!options.length && <span>Нет вариантов</span>}
  </div></fieldset>;
}

function Range({definition: {field, label, step, min, max}, values, onChange}) {
  return <fieldset className="filter-field"><legend>{label}</legend><div className="filter-range">
    {['min', 'max'].map((bound, index) => <label key={bound}>{index === 0 ? 'от' : 'до'}
      <input type="number" inputMode={step === '1' ? 'numeric' : 'decimal'} step={step} min={min} max={max}
        aria-label={`${label}: ${index === 0 ? 'от' : 'до'}`} value={values[`${field}_${bound}`]}
        onChange={e => onChange(`${field}_${bound}`, e.target.value)} />
    </label>)}
  </div></fieldset>;
}

const monthNames = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь',
  'Июль', 'Август', 'Сентябрь', 'Октябрь', 'Ноябрь', 'Декабрь'];

function PeriodPicker({months, error, retry, value, onChange}) {
  const [viewYear, setViewYear] = useState('');
  const years = [...new Set((months || []).map(month => month.slice(0, 4)))];
  const year = [viewYear, value.slice(0, 4)].find(item => years.includes(item)) ?? years[0];
  return <fieldset className="filter-field"><legend>Месяц и год</legend>
    <button type="button" className="period-current" aria-pressed={!value}
      onClick={() => { setViewYear(''); onChange(''); }}>Текущие данные</button>
    {years.length > 0 && <>
      <label className="period-year">Год
        <select value={year} onChange={event => setViewYear(event.target.value)}>
          {years.map(item => <option key={item} value={item}>{item}</option>)}
        </select>
      </label>
      <div className="period-months" role="group" aria-label={`Месяцы за ${year} год`}>
        {monthNames.map((name, index) => {
          const month = `${year}-${String(index + 1).padStart(2, '0')}`;
          const available = months.includes(month);
          const label = `${name} ${year}${available ? '' : ' — нет данных'}`;
          return <button key={month} type="button" disabled={!available} aria-pressed={value === month}
            title={label} aria-label={label} onClick={() => onChange(month)}>{name.slice(0, 3)}</button>;
        })}
      </div>
      <small aria-live="polite">{value
        ? `Выбрано: ${monthNames[Number(value.slice(5)) - 1]} ${value.slice(0, 4)}.`
        : 'Серые месяцы — без данных.'}</small>
    </>}
    {error ? <div role="alert">Не удалось загрузить месяцы. <button type="button" onClick={retry}>Повторить</button></div>
      : !months ? <small role="status">Загрузка месяцев…</small>
      : !years.length && <small>Исторических данных пока нет.</small>}
  </fieldset>;
}

export function Filters({options, onApply, onReset, analytics = false, complexes = analytics, label, propertyKind = "apartment",
  months, monthsError, retryMonths}) {
  const [draft, setDraft] = useState(emptyFilters);
  const [error, setError] = useState('');
  const update = (field, value) => setDraft(current => ({...current, [field]: value}));
  const isHouse = propertyKind === 'house';
  const renderRange = field => {
    const definition = rangeByField[field];
    return <Range key={field} definition={isHouse && field === 'total_area'
      ? {...definition, label: 'Площадь дома, м²'} : definition} values={draft} onChange={update} />;
  };
  function apply(event) {
    event.preventDefault();
    for (const {field, label} of ranges) {
      const low = draft[`${field}_min`], high = draft[`${field}_max`];
      if (low !== '' && high !== '' && Number(low) > Number(high)) {
        setError(`${label}: значение «от» больше значения «до».`);
        return;
      }
    }
    setError('');
    onApply({...draft});
  }
  return <form className={`filters${analytics ? ' analytics-filters' : ''}`} onSubmit={apply}
    aria-label={label || (analytics ? 'Фильтры аналитики' : 'Фильтры объявлений')}>
    <h2>Фильтры</h2>
    <div className="filter-grid">
      {analytics && <PeriodPicker months={months} error={monthsError} retry={retryMonths}
        value={draft.month} onChange={value => update('month', value)} />}
      <label className="filter-field">Актуальность объявлений
        <select value={draft.listing_status} onChange={event => update('listing_status', event.target.value)}>
          <option value="active">Актуальные объявления</option><option value="archived">Только архив</option>
          <option value="all">Все объявления</option>
        </select>
      </label>
      {Object.entries({sale_scope: 'Объект продажи', premise_kind: 'Назначение', construction_status: 'Стадия объекта'}).map(([code, legend]) =>
        <Choices key={code} legend={legend} name={code} options={options?.offer_filters?.[code] || []}
          values={draft[code]} onChange={value => update(code, value)} />)}
      <Choices legend="Район" name="district" options={options?.districts || []}
        values={draft.district} onChange={value => update('district', value)} />
      {propertyKind === "apartment" && complexes && <Choices legend="Жилые комплексы" name="complex_id"
        options={complexChoices(options, String)}
        values={draft.complex_id} onChange={value=>update('complex_id',value)} />}
      {propertyKind === "apartment" && <label className="filter-field">Тип жилья
        <select value={draft.market_type} onChange={e => update('market_type', e.target.value)}>
          <option value="all">Все</option><option value="secondary">Вторичка</option><option value="new">Новостройки</option>
        </select>
      </label>}
      <Choices legend="Количество комнат" name="rooms" options={options?.rooms || []} values={draft.rooms}
        onChange={value => update('rooms', value)} />
      {areaFields[propertyKind].map(renderRange)}
      {!isHouse && <Choices legend="Тип дома" name="house_type" options={asChoices(options?.house_types)}
        values={draft.house_type} onChange={value => update('house_type', value)} />}
      {renderRange('construction_year')}
      {propertyKind === "apartment" && <Choices legend="Пассажирский лифт" name="passenger_elevator"
        options={asChoices(options?.passenger_elevators)}
        values={draft.passenger_elevator} onChange={value => update('passenger_elevator', value)} />}
      <Choices legend="Парковка" name="parking" options={asChoices(options?.parking_options)}
        values={draft.parking} onChange={value => update('parking', value)} />
      {!analytics && propertyKind === "apartment" && <label className="filter-field">Балкон или лоджия
        <select value={draft.balcony} onChange={e => update('balcony', e.target.value)}>
          <option value="all">Все</option><option value="present">Есть балкон или лоджия</option>
          <option value="balcony">Балкон</option><option value="loggia">Лоджия</option>
          <option value="absent">Нет</option><option value="unknown">Не указано</option>
        </select>
      </label>}
      {!analytics && renderRange('ceiling_height')}
      {propertyKind === "apartment" ? <Choices legend="Санузел" name="bathroom" options={[{value: 'combined', label: 'Совмещённый'}, {value: 'separate', label: 'Раздельный'}]}
        values={draft.bathroom} onChange={value => update('bathroom', value)} /> : <Choices legend="Санузел" name="house_bathroom" options={asChoices(options?.house_features?.bathroom)} values={draft.house_bathroom} onChange={value=>update('house_bathroom',value)} />}
      {isHouse && Object.entries(HOUSE_FEATURES).map(([code, legend]) => <Choices key={code} legend={legend} name={code}
        options={asChoices(options?.house_features?.[code])} values={draft[code]} onChange={value => update(code, value)} />)}
      <Choices legend={isHouse ? "Ремонт" : "Отделка"} name="finish" options={asChoices(options?.finishes)}
        values={draft.finish} onChange={value => update('finish', value)} />
      {renderRange('price')}
      {!analytics && renderRange('price_per_sqm')}
    </div>
    {error && <p role="alert">{error}</p>}
    <div className="filter-actions"><button type="submit">Применить фильтры</button>
      <button type="button" onClick={() => { setDraft(emptyFilters()); setError(''); onReset(); }}>Сбросить все фильтры</button>
    </div>
  </form>;
}

function complexPopoverPosition(rect) {
  return {left: Math.max(8, Math.min(rect.left, window.innerWidth - 348)),
    top: Math.max(8, Math.min(rect.bottom + 4, window.innerHeight - 358))};
}

export function ComplexFilter({options, selected, onChange}) {
  const button = useRef(null), panel = useRef(null);
  const [position, setPosition] = useState(null);
  const [search, setSearch] = useState('');
  useEffect(() => {
    if (!position) return;
    const outside = event => {
      if (!panel.current?.contains(event.target) && !button.current?.contains(event.target)) setPosition(null);
    };
    const escape = event => { if (event.key === 'Escape') { setPosition(null); button.current?.focus(); } };
    const scroll = event => {
      if (panel.current?.contains(event.target) || !button.current) return;
      const rect = button.current.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) { setPosition(null); return; }
      const next = complexPopoverPosition(rect);
      setPosition(current => current && current.left === next.left && current.top === next.top ? current : next);
    };
    const resize = () => setPosition(null);
    document.addEventListener('pointerdown', outside);
    document.addEventListener('keydown', escape);
    document.addEventListener('scroll', scroll, true);
    window.addEventListener('resize', resize);
    return () => {
      document.removeEventListener('pointerdown', outside);
      document.removeEventListener('keydown', escape);
      document.removeEventListener('scroll', scroll, true);
      window.removeEventListener('resize', resize);
    };
  }, [position]);
  const visible = complexChoices(options, Number).filter(({label}) => label.toLocaleLowerCase('ru').replaceAll('ё', 'е')
    .includes(search.toLocaleLowerCase('ru').replaceAll('ё', 'е')));
  return <>
    <button ref={button} className="complex-toggle" aria-expanded={!!position} aria-controls="complex-options"
      onClick={() => {
        if (position) { setPosition(null); return; }
        setPosition(complexPopoverPosition(button.current.getBoundingClientRect()));
      }}>Название ЖК{selected.length ? ` (${selected.length})` : ''} ▾</button>
    {position && createPortal(<div ref={panel} id="complex-options" className="complex-popover" style={position}
      role="group" aria-label="Выбор жилых комплексов">
      <strong>Жилые комплексы</strong>
      <input type="search" aria-label="Найти ЖК" placeholder="Найти ЖК" value={search} onChange={e => setSearch(e.target.value)} />
      <div className="complex-options">
        {visible.map(({value, label}) => <label key={value}>
          <input type="checkbox" checked={selected.includes(value)} onChange={e => onChange(e.target.checked
            ? [...selected, value] : selected.filter(item => item !== value))} />{label}
        </label>)}
        {!visible.length && <span>ЖК не найдены.</span>}
      </div>
      <div className="filter-actions"><button type="button" onClick={() => onChange([])}>Все ЖК</button>
        <button type="button" onClick={() => { setPosition(null); button.current?.focus(); }}>Готово</button></div>
    </div>, document.body)}
  </>;
}
