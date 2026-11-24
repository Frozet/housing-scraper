import React, { useEffect, useMemo, useRef, useState } from 'react';
import { api, filterParams } from './api.js';
import { Filters, emptyFilters } from './filters.jsx';
import { chartAxis } from './chart-scale.js';
import { DistributionPanel, SegmentComparison, DuplicateCandidates, formatBin } from './analytics-insights.jsx';
import { formatDate, number } from './format.js';
import { nearestPoint, pointGrid } from './scatter-points.js';

const tickFormat = new Intl.NumberFormat('ru-RU', {maximumSignificantDigits: 3});
const tickNumber = value => tickFormat.format(value);
const valueWithUnit = (value, unit) => value == null ? '—' : `${number(value)} ${unit}`;
const metricRows = [
  {field: 'price', label: 'Цена квартиры', unit: '₽'},
  {field: 'price_per_sqm', label: 'Цена за м²', unit: '₽/м²'},
  {field: 'total_area', label: 'Общая площадь', unit: 'м²'},
];

function Mode({metric, unit}) {
  if (!metric.count) return '—';
  if (!metric.modes.length) return 'Нет моды';
  const values = metric.modes.map(value => valueWithUnit(value, unit));
  return <>
    {values.length <= 3 ? values.join('; ') : <details>
      <summary>{values.length} значений — показать все</summary>
      <div className="mode-values">{values.join('; ')}</div>
    </details>}
    <small className="mode-frequency">Частота каждого: {metric.mode_frequency}</small>
  </>;
}

function priceScale(maximum, label, suffix = '₽') {
  if (maximum >= 1_000_000) return {divisor: 1_000_000, label: `${label}, млн ${suffix}`};
  if (maximum >= 1_000) return {divisor: 1_000, label: `${label}, тыс. ${suffix}`};
  return {divisor: 1, label: `${label}, ${suffix}`};
}

function axisUnits(field, axis, propertyKind) {
  if (field === 'total_area') return {divisor: 1, label: propertyKind === 'house' ? 'Площадь дома, м²' : 'Общая площадь, м²'};
  return field === 'price_per_sqm'
    ? priceScale(axis.mainMax, 'Цена за м²', '₽/м²')
    : priceScale(axis.mainMax, propertyKind === 'house' ? 'Цена дома' : 'Цена квартиры');
}

function ScatterPoints({points, width, height}) {
  const canvas = useRef(null);
  const [hovered, setHovered] = useState(null);
  const grid = useMemo(() => pointGrid(points), [points]);
  useEffect(() => {
    const layer = canvas.current;
    const ratio = window.devicePixelRatio || 1;
    layer.width = Math.ceil(width * ratio);
    layer.height = Math.ceil(height * ratio);
    const context = layer.getContext('2d');
    if (!context) return;
    context.scale(ratio, ratio);
    context.fillStyle = '#000';
    context.globalAlpha = .35;
    // Draw separately so overlapping offers still darken the same pixel.
    for (const {x, y} of points) {
      context.beginPath();
      context.arc(x, y, 1.3, 0, Math.PI * 2);
      context.fill();
    }
  }, [points, width, height]);
  return <>
    <canvas ref={canvas} className="scatter-points" aria-hidden="true"
      onPointerLeave={() => setHovered(null)}
      onPointerMove={event => {
        const rect = event.currentTarget.getBoundingClientRect();
        const x = (event.clientX - rect.left) * width / rect.width;
        const y = (event.clientY - rect.top) * height / rect.height;
        const point = nearestPoint(grid, x, y);
        setHovered(point ? {point, grid} : null);
      }} />
    {hovered?.grid === grid && <div className="scatter-tooltip" role="tooltip"
      style={{left: `${hovered.point.x / width * 100}%`, top: hovered.point.y + 8,
        transform: hovered.point.x > width / 2 ? 'translateX(-100%)' : undefined}}>
      № {hovered.point.avito_id} · {valueWithUnit(hovered.point.price, '₽')} ·{' '}
      {valueWithUnit(hovered.point.total_area, 'м²')} · {valueWithUnit(hovered.point.price_per_sqm, '₽/м²')}
    </div>}
  </>;
}

function Scatter({points, field, yField = 'price', title, scaleMode, propertyKind}) {
  const container = useRef(null);
  const [width, setWidth] = useState(820);
  useEffect(() => {
    const observer = new ResizeObserver(([entry]) => {
      const next = Math.round(entry.contentRect.width);
      // A hidden tab has zero width; keep its last useful dimensions.
      if (next > 0) setWidth(current => current === next ? current : next);
    });
    observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  const height = Math.round(Math.max(300, Math.min(440, width * 0.36)));
  const left = width < 500 ? 48 : 58, right = 18, top = 30, bottom = 44;
  const plotWidth = Math.max(1, width - left - right), plotHeight = height - top - bottom;
  const {xAxis, yAxis} = useMemo(() => ({
    xAxis: chartAxis(points.map(point => point[field]), plotWidth, Math.max(3, Math.floor(plotWidth / 70)), scaleMode),
    yAxis: chartAxis(points.map(point => point[yField]), plotHeight, 10, scaleMode),
  }), [points, field, yField, plotWidth, plotHeight, scaleMode]);
  const id = `chart-${field}-${yField}`;
  const xUnits = axisUnits(field, xAxis, propertyKind);
  const yUnits = axisUnits(yField, yAxis, propertyKind);
  const x = value => left + xAxis.position(value) * plotWidth;
  const y = value => height - bottom - yAxis.position(value) * plotHeight;
  const plotted = useMemo(() => points.map(point => ({...point,
    x: left + xAxis.position(point[field]) * plotWidth,
    y: height - bottom - yAxis.position(point[yField]) * plotHeight,
  })), [points, field, yField, left, xAxis, plotWidth, height, yAxis, plotHeight]);
  const compression = [
    xAxis.compressed && 'по горизонтали',
    yAxis.compressed && 'по вертикали',
  ].filter(Boolean);
  const scaleDescription = compression.length ? `Плавная нелинейная шкала ${compression.join(' и ')}. Деления расположены равномерно, шаг значений увеличивается к краю шкалы.` : 'Масштаб обеих осей линейный.';
  return <section className="scatter" ref={container}>
    <h2>{title}</h2>
    {!points.length ? <p>Нет объявлений с ценой для построения графика.</p> : <>
    <div className="scatter-plot">
    <svg width={width} height={height} role="img" aria-labelledby={`${id}-title ${id}-description`}>
      <title id={`${id}-title`}>{title}</title>
      <desc id={`${id}-description`}>Точек: {points.length}. По горизонтали: {xUnits.label}. По вертикали: {yUnits.label}. Каждая точка — одно объявление. {scaleDescription}</desc>
      {yAxis.ticks.map(tick => <g key={tick}>
        <line className="chart-grid" x1={left} x2={width - right} y1={y(tick)} y2={y(tick)} />
        <text x={left - 10} y={y(tick) + 4} textAnchor="end">{tickNumber(tick / yUnits.divisor)}</text>
      </g>)}
      {xAxis.ticks.map(tick => <g key={tick}>
        <line className="chart-grid" x1={x(tick)} x2={x(tick)} y1={top} y2={height - bottom} />
        <text x={x(tick)} y={height - bottom + 18} textAnchor={tick === xAxis.max ? 'end' : 'middle'}>{tickNumber(tick / xUnits.divisor)}</text>
      </g>)}
      <path d={`M${left} ${top}V${height - bottom}H${width - right}`} fill="none" stroke="black" />
      <text x={left} y={14}>{yUnits.label}</text>
      <text x={left + plotWidth / 2} y={height - 7} textAnchor="middle">{xUnits.label}</text>
    </svg>
    <ScatterPoints points={plotted} width={width} height={height} />
    </div>
    <p className="note chart-scale-note">{scaleDescription} Все {number(points.length)} точек сохранены. Наведите курсор на точку, чтобы увидеть значения.</p>
    </>}
  </section>;
}

export function Analytics({active, city, propertyKind}) {
  const [filters, setFilters] = useState(emptyFilters);
  const [options, setOptions] = useState(null);
  const [optionsError, setOptionsError] = useState('');
  const [months, setMonths] = useState(null);
  const [monthsError, setMonthsError] = useState('');
  const [revision, setRevision] = useState(0);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [scaleMode, setScaleMode] = useState('auto');
  const [budget, setBudget] = useState({budget_min:'', budget_max:''});
  const [compareBy, setCompareBy] = useState('rooms');
  const [histogramField, setHistogramField] = useState('price');
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    api(`/analytics/months?${filterParams({city_id:city.id, property_kind:propertyKind})}`, {signal: controller.signal})
      .then(result => { if (!controller.signal.aborted) { setMonths(result.months); setMonthsError(''); } })
      .catch(e => { if (!controller.signal.aborted) setMonthsError(e.message); });
    return () => controller.abort();
  }, [active, revision, city.id, propertyKind]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    api(`/listings/filter-options?${filterParams({city_id:city.id, property_kind:propertyKind, listing_status:'all',month:filters.month || ''})}`, {signal: controller.signal})
      .then(result => { if (!controller.signal.aborted) { setOptions(result); setOptionsError(''); } })
      .catch(e => { if (e.name !== 'AbortError') setOptionsError(e.message); });
    return () => controller.abort();
  }, [active, revision, city.id, filters.month, propertyKind]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    setLoading(true);
    setError('');
    api(`/analytics?${filterParams({...filters,...budget,compare_by:compareBy,city_id:city.id,property_kind:propertyKind})}`, {signal: controller.signal})
      .then(result => { if (!controller.signal.aborted) { setData(result); setLoading(false); } })
      .catch(e => { if (e.name !== 'AbortError') { setError(e.message); setLoading(false); } });
    return () => controller.abort();
  }, [active, filters, budget, compareBy, revision, city.id, propertyKind]);
  const definitions = [...metricRows.map(row => ({...row, label: propertyKind === 'house' ? row.field === 'price' ? 'Цена дома' : row.field === 'total_area' ? 'Площадь дома' : row.label : row.label})),
    ...(propertyKind === 'house' ? [{field:'land_area',label:'Площадь участка',unit:'сот.'}] : [])].map(row => {
      const units = row.field.startsWith('price') ? priceScale(data?.metrics[row.field]?.median || 0, row.label, row.unit) : {divisor:1,label:`${row.label}, ${row.unit}`};
      return {...row, divisor:units.divisor, axisLabel:units.label};
    });
  return <>
    <h1>Аналитика · {propertyKind === "house" ? "Дома" : "Квартиры"} — {city.name}</h1>
    <div className="analytics-layout">
      <aside>
        <Filters propertyKind={propertyKind} analytics options={options} months={months} monthsError={monthsError}
          retryMonths={() => setRevision(value => value + 1)} onApply={setFilters} onReset={() => setFilters(emptyFilters())} />
        {optionsError && <p role="alert">Не удалось загрузить списки фильтров: {optionsError}
          <button onClick={() => setRevision(value => value + 1)}>Повторить загрузку списков</button></p>}
      </aside>
      <div className="analytics-results" aria-busy={loading}>
        <div className="analytics-toolbar"><h2>Показатели выборки</h2>
          <button onClick={() => setRevision(value => value + 1)}>Пересчитать</button></div>
        {loading && <p role="status">Расчёт показателей…</p>}
        {error && <p className="error" role="alert">Не удалось рассчитать показатели: {error}</p>}
        {!loading && !error && data && <>
          <p role="status">Найдено объявлений: <strong>{number(data.total)}</strong>. С ценой: {number(data.priced_count)}.
            {' '}Рассчитано: {formatDate(data.calculated_at)} (Иркутск).</p>
          {data.period && <p className="note">Последнее известное состояние {data.period.complete ? 'на конец месяца:' : 'за месяц:'}{' '}
            {new Intl.DateTimeFormat('ru-RU', {timeZone:'Asia/Irkutsk',month:'long',year:'numeric'}).format(new Date(`${data.period.month}-15T12:00:00+08:00`))}
            {!data.period.complete && ' Месяц ещё не завершён; учтены сохранённые наблюдения к текущему моменту.'}</p>}
          {data.total === 0 && <p>По выбранным фильтрам объявления не найдены.</p>}
          <p className="note">Цены предложений загруженной базы, а не цены сделок.</p>
          <details className="selection-quality"><summary>Состав выборки и качество данных</summary>
            {Object.entries({sale_scope:'Объект продажи',premise_kind:'Назначение',construction_status:'Стадия объекта'}).map(([code,label]) =>
              <p key={code}><strong>{label}:</strong> {data.quality.offers[code].map(item => `${item.label}: ${number(item.count)}`).join(' · ')}</p>)}
            <p>Район указан: {number(data.quality.district_known)} из {number(data.total)}. Недостающие сведения появятся при обновлении объявлений, если их передаст Авито.</p>
          </details>
          <div className="metrics-scroll"><table className="metrics insight-metrics" aria-label="Распределение показателей выборки">
            <thead><tr><th scope="col">Показатель</th><th scope="col">P10</th><th scope="col">P25</th><th scope="col">Медиана</th><th scope="col">P75</th><th scope="col">P90</th><th scope="col">Среднее</th><th scope="col">Частый диапазон</th><th scope="col">Учтено</th></tr></thead>
            <tbody>{definitions.map(({field, axisLabel, divisor}) => {
              const metric = data.metrics[field];
              const distribution = data.distributions[field];
              return <tr key={field}><th scope="row">{axisLabel}</th>
                {['p10','p25','median','p75','p90','mean'].map(key => {
                  const value = ['median','mean'].includes(key) ? metric[key] : distribution[key];
                  return <td key={key}>{key === 'median' ? <strong>{value == null ? '—' : number(value / divisor)}</strong> : value == null ? '—' : number(value / divisor)}</td>;
                })}
                <td>{distribution.popular_ranges.length > 2 ? <details><summary>{distribution.popular_ranges.length} диапазонов</summary>
                  {distribution.popular_ranges.map((bin,index) => <div key={index}>{formatBin(bin, divisor)}</div>)}</details> : distribution.popular_ranges.map(bin => formatBin(bin, divisor)).join('; ') || '—'}</td>
                <td>{number(metric.count)}</td></tr>;
            })}</tbody>
          </table></div>
          <p className="note">P25–P75 — центральная половина предложений; P10–P90 — центральные 80%. Единицы указаны в названии строки.</p>
          <DistributionPanel data={data} definitions={definitions} selected={histogramField} onSelect={setHistogramField} budget={budget} onBudget={setBudget} />
          <SegmentComparison comparison={data.comparison} selected={compareBy} onSelect={setCompareBy} historical={Boolean(data.period)} propertyKind={propertyKind} />
          <DuplicateCandidates filters={filters} cityId={city.id} propertyKind={propertyKind} count={data.quality.duplicate_groups} />
          <details className="analysis-section"><summary>Дополнительные показатели: точная мода</summary>
            <div className="metrics-scroll"><table className="metrics"><thead><tr><th>Показатель</th><th>Мода</th><th>Учтено</th></tr></thead>
              <tbody>{definitions.map(({field,label,unit}) => <tr key={field}><th scope="row">{label}</th><td><Mode metric={data.metrics[field]} unit={unit} /></td><td>{number(data.metrics[field].count)}</td></tr>)}</tbody>
            </table></div>
          </details>
          <div className="chart-settings"><label>Масштаб графиков
            <select value={scaleMode} onChange={event => setScaleMode(event.target.value)}>
              <option value="auto">Автоматический — сжатие далёких значений</option>
              <option value="linear">Линейный</option>
            </select></label></div>
          <Scatter points={data.points} field="total_area" propertyKind={propertyKind} title={propertyKind === "house" ? "Цена дома и площадь дома" : "Цена квартиры и общая площадь"} scaleMode={scaleMode} />
          <Scatter points={data.points} field="price_per_sqm" propertyKind={propertyKind} title={propertyKind === "house" ? "Цена дома и цена за м²" : "Цена квартиры и цена за м²"} scaleMode={scaleMode} />
          <Scatter points={data.points} field="total_area" yField="price_per_sqm" propertyKind={propertyKind} title={propertyKind === "house" ? "Цена за м² и площадь дома" : "Цена за м² и общая площадь"} scaleMode={scaleMode} />
        </>}
      </div>
    </div>
  </>;
}
