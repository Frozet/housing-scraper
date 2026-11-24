import React, {useEffect, useRef, useState} from 'react';
import {api, filterParams} from './api.js';
import {number, unitMoney as money} from './format.js';
const valueOrDash = value => value == null ? '—' : number(value);

export function formatBin(bin, divisor = 1) {
  if (!bin) return '—';
  if (bin.kind === 'below') return `< ${number(bin.upper / divisor)}`;
  if (bin.kind === 'above') return `≥ ${number(bin.lower / divisor)}`;
  if (bin.kind === 'exact') return number(bin.lower / divisor);
  return `${number(bin.lower / divisor)}–${bin.upper_inclusive ? '' : '<'}${number(bin.upper / divisor)}`;
}

function Histogram({distribution, definition}) {
  const container = useRef(null);
  const [width, setWidth] = useState(820);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => { if (entry.contentRect.width > 0) setWidth(Math.round(entry.contentRect.width)); });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const bins = distribution.bins;
  const height = 230, left = 46, right = 18, top = 15, bottom = 48;
  const plotWidth = Math.max(1, width - left - right), plotHeight = height - top - bottom;
  const step = Math.max(1, Math.ceil(Math.max(0, ...bins.map(bin => bin.count)) / 4));
  const ymax = step * 4, cell = plotWidth / Math.max(1, bins.length);
  const labelEvery = Math.max(1, Math.ceil(bins.length * 58 / plotWidth));
  const y = count => height - bottom - plotHeight * count / ymax;
  return <div className="histogram" ref={container}>
    {!distribution.count ? <p>Нет значений для гистограммы.</p> : <>
      <svg width={width} height={height} role="img" aria-label={`Гистограмма: ${definition.label}. Учтено ${distribution.count} объявлений.`}>
        <desc>По вертикали — число объявлений, по горизонтали — диапазоны значений. Наведение показывает границы и число объявлений. Все значения учтены.</desc>
        {Array.from({length: 5}, (_, index) => index * step).map(tick => <g key={tick}>
          <line className="chart-grid" x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} />
          <text x={left - 6} y={y(tick) + 4} textAnchor="end">{number(tick)}</text>
        </g>)}
        {bins.map((bin, index) => <g key={index}>
          <rect x={left + index * cell + 1} y={y(bin.count)} width={Math.max(1, cell - 2)} height={plotHeight * bin.count / ymax}>
            <title>{`${formatBin(bin, definition.divisor)} · ${bin.count} объявлений (${number(bin.count * 100 / distribution.count)}%)`}</title>
          </rect>
          {((index % labelEvery === 0 && bins.length - 1 - index >= labelEvery) || index === bins.length - 1) && <text x={left + (index + .5) * cell} y={height - bottom + 18}
            textAnchor={index === bins.length - 1 ? 'end' : 'middle'}>{bin.kind === 'below' ? `<${number(bin.upper / definition.divisor)}` : `${bin.kind === 'above' ? '≥' : ''}${number(bin.lower / definition.divisor)}`}</text>}
        </g>)}
        <path d={`M${left} ${top}V${height - bottom}H${width - right}`} fill="none" stroke="black" />
        <text x={left} y={11}>Объявлений</text>
        <text x={left + plotWidth / 2} y={height - 6} textAnchor="middle">{definition.axisLabel}</text>
      </svg>
      <p className="note">Учтено: {number(distribution.count)}. Границы и число объявлений — при наведении.
        {bins.some(bin => ['below', 'above'].includes(bin.kind)) && ' Далёкие значения показаны отдельными крайними столбцами < / ≥.'}</p>
    </>}
  </div>;
}

export function DistributionPanel({data, definitions, selected, onSelect, budget, onBudget}) {
  const [draft, setDraft] = useState(budget);
  const [error, setError] = useState('');
  function apply(event) {
    event.preventDefault();
    const low = draft.budget_min, high = draft.budget_max;
    if ([low, high].some(value => value !== '' && (!Number.isSafeInteger(Number(value)) || Number(value) < 0))) {
      setError('Укажите бюджет целым неотрицательным числом.'); return;
    }
    if (low !== '' && high !== '' && Number(low) > Number(high)) { setError('Бюджет: «от» больше «до».'); return; }
    setError('');
    onBudget(Object.fromEntries(Object.entries(draft).map(([key, value]) => [key, value === '' ? '' : String(Number(value))])));
  }
  const definition = definitions.find(row => row.field === selected) || definitions[0];
  return <section className="analysis-section">
    <h2>Распределение предложений</h2>
    <form className="budget-form" onSubmit={apply} aria-label="Доля объявлений в бюджете">
      <label>Бюджет от, ₽ <input type="number" min="0" step="1" value={draft.budget_min} onChange={event => setDraft({...draft, budget_min: event.target.value})} /></label>
      <label>до, ₽ <input type="number" aria-label="Бюджет до, ₽" min="0" step="1" value={draft.budget_max} onChange={event => setDraft({...draft, budget_max: event.target.value})} /></label>
      <button>Рассчитать долю</button>
      <button type="button" onClick={() => {const empty = {budget_min:'', budget_max:''}; setDraft(empty); setError(''); onBudget(empty);}}>Сбросить бюджет</button>
    </form>
    {error && <p role="alert">{error}</p>}
    <p role="status">{!data.budget.configured ? 'Укажите бюджет, чтобы узнать долю предложений в текущей выборке.' : !data.budget.priced_count ? 'В выборке нет объявлений с ценой.' : <>
      В бюджете: <strong>{number(data.budget.count)} из {number(data.budget.priced_count)} ({number(data.budget.share)}%)</strong>.
      {' '}Бюджет не исключает объявления из расчётов и графиков.</>}</p>
    <label className="insight-selector">Гистограмма
      <select value={definition.field} onChange={event => onSelect(event.target.value)}>{definitions.map(row => <option key={row.field} value={row.field}>{row.label}</option>)}</select>
    </label>
    <Histogram distribution={data.distributions[definition.field]} definition={definition} />
  </section>;
}

export function SegmentComparison({comparison, selected, onSelect, historical, propertyKind}) {
  const options = {rooms:'Комнаты', district:'Районы', complex:'Жилые комплексы', area:'Площадь',
    finish: propertyKind === 'house' ? 'Ремонт' : 'Отделка', building_age:'Возраст здания',
    building_type: propertyKind === 'house' ? 'Материал стен' : 'Тип здания', market_type:'Первичный / вторичный рынок', offer:'Категории предложений'};
  return <section className="analysis-section">
    <div className="analytics-toolbar"><h2>Сравнение сегментов</h2><label className="insight-selector">Сравнить по
      <select value={selected} onChange={event => onSelect(event.target.value)}>{Object.entries(options).filter(([key]) => propertyKind !== 'house' || key !== 'complex').map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select>
    </label></div>
    {!comparison.rows.length ? <p>Нет сегментов для сравнения.</p> : <div className="comparison-scroll"><table aria-label="Сравнение сегментов">
      <thead><tr><th>Сегмент</th><th>Количество</th><th>Медиана цены</th><th>Медиана ₽/м²</th><th>P25–P75 цены, ₽</th><th>P25–P75 ₽/м²</th><th>{historical ? 'Давность изменения снимка' : 'Давность проверки'}</th></tr></thead>
      <tbody>{comparison.rows.map(row => <tr key={row.key}><th scope="row">{row.label}</th><td>{number(row.count)}<small>с ценой: {number(row.priced_count)}</small></td>
        <td>{money(row.median_price)}</td><td>{money(row.median_price_per_sqm)}</td>
        <td>{valueOrDash(row.p25_price)} — {valueOrDash(row.p75_price)}</td><td>{valueOrDash(row.p25_price_per_sqm)} — {valueOrDash(row.p75_price_per_sqm)}</td>
        <td>{row.freshness.known_count ? <>{number(row.freshness.recent_share)}% ≤ 30 дней<small>Медиана: {number(row.freshness.median_days)} дн.</small></> : 'Нет даты'}</td>
      </tr>)}</tbody>
    </table></div>}
    <p className="note">{historical ? 'В истории учтены изменения состояния; проверки без изменений не записаны.' : 'Давность считается от последней проверки объявления.'}
      {comparison.overlapping && ' При нескольких характеристиках одно объявление может входить в несколько строк.'}
      {' '}Разница медиан зависит от локации, площади и состояния объектов.</p>
  </section>;
}

function DuplicateGroup({group, readOnly, onRefresh}) {
  const [leftId, setLeft] = useState(String(group.members[0].id));
  const [rightId, setRight] = useState(String(group.members[1].id));
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [localMark, setLocalMark] = useState(null);
  useEffect(() => setLocalMark(null), [group]);
  const left = group.members.find(item => item.id === Number(leftId)), right = group.members.find(item => item.id === Number(rightId));
  const pair = [Number(leftId), Number(rightId)].sort((a,b) => a-b);
  const mark = [localMark, ...group.reviews].find(item => item?.left_id === pair[0] && item?.right_id === pair[1]);
  async function review(decision) {
    setBusy(true); setError('');
    try {
      const result = await api('/analytics/duplicates/review', {method:'POST', headers:{'Content-Type':'application/json'},
        body:JSON.stringify({left_id:left.id, right_id:right.id, left_signature:left.signature, right_signature:right.signature, decision})});
      setLocalMark(result); onRefresh();
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  return <details className="duplicate-group"><summary>{group.address} · {number(group.total_area)} м² · {group.members.length} объявлений</summary>
    <div className="duplicate-members"><table><thead><tr><th>Объявление</th><th>Предложение</th><th>Комнаты / этаж</th><th>ЖК</th><th>Цена</th></tr></thead>
      <tbody>{group.members.map(item => <tr key={item.id}><td><a href={item.url} target="_blank" rel="noreferrer">№ {item.avito_id}</a><small>{item.title}</small></td>
        <td>{item.offer_category}</td><td>{item.rooms === 0 ? 'Студия' : item.rooms ?? '—'} / {item.floor ?? '—'}{item.floors_total ? ` из ${item.floors_total}` : ''}</td><td>{item.complex_name || '—'}</td><td>{money(item.price)}</td></tr>)}</tbody>
    </table></div>
    <div className="duplicate-review">
      {[['Первое объявление',leftId,setLeft],['Второе объявление',rightId,setRight]].map(([label,value,setValue]) => <label key={label}>{label}
        <select value={value} onChange={event => {setValue(event.target.value); setError('');}}>{group.members.map(item => <option key={item.id} value={item.id}>№ {item.avito_id} · {item.title}</option>)}</select>
      </label>)}
      <span role="status">{mark?.stale ? 'Характеристики изменились — нужна повторная проверка' : ({same:'Отмечено: один объект',different:'Отмечено: разные объекты',pending:'Не проверено'}[mark?.decision] || 'Не проверено')}</span>
      <div className="filter-actions">{[['same','Один объект'],['different','Разные объекты'],['pending','Отменить отметку']].map(([value,label]) =>
        <button key={value} disabled={busy || readOnly || leftId === rightId} onClick={() => review(value)}>{label}</button>)}</div>
    </div>
    {error && <p role="alert">{error}</p>}
  </details>;
}

export function DuplicateCandidates({filters, cityId, propertyKind, count}) {
  const [open, setOpen] = useState(false), [page, setPage] = useState(1), [revision, setRevision] = useState(0);
  const [data, setData] = useState(null), [error, setError] = useState(''), [loading, setLoading] = useState(false);
  const scope = filterParams({...filters, city_id:cityId, property_kind:propertyKind}).toString();
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController(); setLoading(true); setError('');
    api(`/analytics/duplicates?${scope}&duplicate_page=${page}`, {signal:controller.signal})
      .then(result => {if (!controller.signal.aborted) {setData(result); setLoading(false);}})
      .catch(e => {if (e.name !== 'AbortError') {setError(e.message); setLoading(false);}});
    return () => controller.abort();
  }, [open, scope, page, revision]);
  return <details className="analysis-section" open={open} onToggle={event => setOpen(event.currentTarget.open)}>
    <summary>Проверка возможных дублей · групп: {number(count)}</summary>
    <p className="note">Совпали адрес здания и точная площадь. Это только кандидаты: одинаковые квартиры в новостройке могут быть разными объектами.
      {' '}Проверка сохраняется для пары объявлений. Объявления и история цен остаются раздельными.</p>
    {loading && <p role="status">Загрузка кандидатов…</p>}
    {error && <p role="alert">{error} <button onClick={() => setRevision(value => value + 1)}>Повторить</button></p>}
    {data && <>
      {data.historical && <p className="note">Для выбранного месяца доступен просмотр. Проверяйте пары на текущих данных.</p>}
      {!data.total_groups && <p>Совпадения по адресу здания и площади не найдены.</p>}
      {data.groups.map(group => <DuplicateGroup key={group.key} group={group} readOnly={data.historical} onRefresh={() => setRevision(value => value + 1)} />)}
      {data.total_groups > data.page_size && <div className="pagination"><button disabled={loading || page <= 1} onClick={() => setPage(value => value - 1)}>Назад</button>
        <span>Группы: страница {page} из {Math.ceil(data.total_groups / data.page_size)}</span>
        <button disabled={loading || page * data.page_size >= data.total_groups} onClick={() => setPage(value => value + 1)}>Далее</button></div>}
    </>}
  </details>;
}
