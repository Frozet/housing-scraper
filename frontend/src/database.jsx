import React, {useCallback, useEffect, useRef, useState} from 'react';
import {ComplexFilter, Filters, emptyFilters} from './filters.jsx';
import {CityPicker} from './cities.jsx';
import {api, filterParams} from './api.js';
import {allColumnKeys, decodeColumnSettings, toggleColumn, columnsFor, columnSettingsKey} from './table-columns.js';
import {money, unitMoney, formatDate as date} from './format.js';
import {useLatestRequest} from './latest-request.js';

const show = value => value == null || value === '' ? '—' : String(value);
const roomCount = row => {
  const count = row.rooms === 0 ? 'Студия' : show(row.rooms);
  return row.is_open_plan ? `${row.rooms == null ? '' : `${count} · `}Свободная планировка` : count;
};
const active = run => ['queued', 'running'].includes(run?.status);
const statuses = {queued: 'В очереди', running: 'Сбор идёт', completed: 'Завершён', partial: 'Частично завершён',
  blocked: 'Доступ Авито ограничен', failed: 'Ошибка', interrupted: 'Прерван'};
function savedColumns(kind) {
  try { return decodeColumnSettings(localStorage.getItem(columnSettingsKey(kind)), kind); }
  catch { return allColumnKeys(kind); }
}

function History({ listing, onClose }) {
  const dialog = useRef(null);
  const [data, setData] = useState(null);
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    dialog.current.showModal();
    api(`/listings/${listing.id}/prices`, {signal: controller.signal}).then(setData)
      .catch(e => { if (e.name !== 'AbortError') setError(e.message); });
    return () => controller.abort();
  }, [listing.id]);
  return <dialog ref={dialog} onClose={onClose} aria-labelledby="history-title">
    <div className="dialog-header"><h2 id="history-title">История цены · № {listing.avito_id}</h2>
      <button onClick={() => dialog.current.close()}>Закрыть</button></div>
    <p>{listing.title}</p>
    <p>Дата наблюдения — когда сборщик впервые увидел эту цену. Время указано по Иркутску.</p>
    {error ? <p role="alert">{error}</p> : !data ? <p role="status">Загрузка…</p> : <>
      <table><thead><tr><th>Дата наблюдения</th><th>Цена</th><th>Изменение</th><th>Дата обновления Авито</th></tr></thead>
        <tbody>{data.items.map(item => <tr key={item.id}><td>{date(item.observed_at)}</td><td>{money(item.amount)}</td>
          <td>{item.change == null ? 'Начальная цена' : `${item.change > 0 ? '+' : '−'}${money(Math.abs(item.change))}`}</td>
          <td>{date(item.source_updated_at)}</td></tr>)}</tbody></table>
      <p>Последняя проверка объявления: {date(data.last_seen_at)}. Одинаковые цены при повторных проверках не дублируются.</p>
    </>}
  </dialog>;
}

function HouseRadius({city, onRefresh, disabled}) {
  const [radius,setRadius] = useState(city.house_radius_km), [error,setError] = useState(''), [saving,setSaving] = useState(false);
  useEffect(()=>setRadius(city.house_radius_km),[city.house_radius_km]);
  async function save(value) {
    setSaving(true); setError('');
    try { await api(`/cities/${city.id}/house-radius`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({radius_km:value})}); await onRefresh(); }
    catch(e) {setError(e.message);} finally {setSaving(false);}
  }
  return <div className="house-radius"><form onSubmit={event=>{event.preventDefault();save(Number(radius));}}>
    <label>Радиус от центра города, км <input type="number" min="5" max="100" step="1" required value={radius} onChange={event=>setRadius(event.target.value)} /></label>
    <button disabled={disabled || saving}>Сохранить радиус</button><button type="button" disabled={disabled || saving} onClick={()=>save(null)}>Автоматически</button>
    <span>{city.radius_is_custom ? 'Задан вручную' : 'Рассчитан по размеру города'} · {city.house_radius_km} км</span>
  </form>{error && <p role="alert">{error}</p>}</div>;
}

export function Database({city, cities, onSelectCity, onRefreshCities, propertyKind, visible}) {
  const tableColumns = columnsFor(propertyKind);
  const [data, setData] = useState({items: [], total: 0, page_size: 100});
  const [run, setRun] = useState(null);
  const [globalRun, setGlobalRun] = useState(null);
  const [progress, setProgress] = useState(null);
  const [resumeMode, setResumeMode] = useState('');
  const [resumable, setResumable] = useState([]);
  const statusVersion = useRef('');
  const shownDataVersion = useRef(null);
  const modeNames = {full: 'Полный обход', sample: 'Выборка 1000', refresh: 'Обновление объявлений'};
  const [page, setPage] = useState(1);
  const [sort, setSort] = useState({field: 'last_seen_at', direction: 'desc'});
  const [filters, setFilters] = useState(emptyFilters);
  const [complexIds, setComplexIds] = useState([]);
  const [filterOptions, setFilterOptions] = useState(null);
  const [optionsError, setOptionsError] = useState('');
  const [beginListings, abortListings] = useLatestRequest();
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(true);
  const [starting, setStarting] = useState(false);
  const [history, setHistory] = useState(null);
  const [filtersVisible, setFiltersVisible] = useState(false);
  const [fieldsVisible, setFieldsVisible] = useState(false);
  const [columnKeys, setColumnKeys] = useState(() => savedColumns(propertyKind));
  const visibleColumns = tableColumns.filter(column => columnKeys.includes(column.key));
  useEffect(() => {
    try { localStorage.setItem(columnSettingsKey(propertyKind), JSON.stringify(columnKeys)); }
    catch { /* Column selection still works when browser storage is unavailable. */ }
  }, [columnKeys, propertyKind]);
  const refresh = useCallback(async () => {
    const signal = beginListings();
    const params = filterParams({...filters, city_id:city.id, property_kind:propertyKind});
    for (const [key, value] of Object.entries({page, page_size: 100, sort_by: sort.field, sort_order: sort.direction})) params.set(key, value);
    complexIds.forEach(value => params.append('complex_id', value));
    const result = await api(`/listings?${params}`, {signal});
    if (!signal.aborted) {
      setData(result);
      setLoading(false);
      return true;
    }
    return false;
  }, [beginListings, page, sort, filters, complexIds, city.id, propertyKind]);
  const refreshOptions = useCallback(async signal => {
    try {
      const result = await api(`/listings/filter-options?city_id=${city.id}&property_kind=${propertyKind}&listing_status=all`, {signal});
      if (!signal?.aborted) { setFilterOptions(result); setOptionsError(''); }
    } catch (e) { if (e.name !== 'AbortError') setOptionsError(e.message); }
  }, [city.id, propertyKind]);
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    refreshOptions(controller.signal);
    return () => controller.abort();
  }, [visible, refreshOptions, run?.id, run?.status]);
  // Poll the small status response; reload the table only for a new query or new data.
  useEffect(() => {
    if (!visible) return;
    const controller = new AbortController();
    const statusParams = {city_id: city.id, property_kind: propertyKind, collection_mode: resumeMode};
    let busy = false;
    shownDataVersion.current = null;
    const poll = async () => {
      if (busy) return;
      busy = true;
      try {
        const status = await api(`/collection-status?${filterParams({...statusParams, since: statusVersion.current})}`,
          {signal: controller.signal});
        if (controller.signal.aborted) return;
        setRun(status.run);
        setGlobalRun(status.global_run);
        statusVersion.current = status.version;
        if (status.progress) setProgress(status.progress);
        if (status.resumable) {
          setResumable(status.resumable);
          if (resumeMode && !status.resumable.some(item => item.mode === resumeMode)) setResumeMode('');
        }
        if (shownDataVersion.current !== status.data_version && await refresh()) {
          shownDataVersion.current = status.data_version;
        }
        if (!controller.signal.aborted) setError('');
      } catch (e) {
        if (!controller.signal.aborted && e.name !== 'AbortError') {
          setError(e.message);
          setLoading(false);
        }
      } finally {
        busy = false;
      }
    };
    setLoading(true);
    poll();
    const timer = setInterval(poll, 2500);
    return () => { controller.abort(); clearInterval(timer); abortListings(); };
  }, [visible, refresh, abortListings, city.id, propertyKind, resumeMode]);
  function changeSort(field) {
    setSort(current => ({field, direction: current.field === field && current.direction === 'asc' ? 'desc' : 'asc'}));
    setPage(1);
  }
  const selectedResume = resumable.find(item => item.mode === resumeMode) || resumable[0];
  const canResume = Boolean(selectedResume || progress?.can_resume);
  async function start(mode = 'resume', collectionMode = selectedResume?.mode || (progress?.can_resume && progress.mode) || 'full') {
    setStarting(true);
    setError('');
    try { setRun(await api('/runs', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({mode,city_id:city.id,collection_mode:collectionMode,property_kind:propertyKind})})); }
    catch (e) { setError(e.message); }
    finally { setStarting(false); }
  }
  const feature = (row, code) => row.features[code]?.join(', ') || '—';
  function cell(row, column) {
    const key = column.key;
    if (column.feature) return feature(row, key);
    switch (key) {
      case 'rooms': return roomCount(row);
      case 'listing_status': return <span title={row.archive_reason || undefined}>{row.is_archived ? `Архив · ${date(row.archived_at)}` : 'Актуальное'}</span>;
      case 'floor': return row.floor == null ? '—' : `${row.floor}${row.floors_total ? ` / ${row.floors_total}` : ''}`;
      case 'price': return <button className="price" onClick={() => setHistory(row)} aria-label={`История цены объявления ${row.avito_id}`}>
        {money(row.price)}<small>{Math.max(0, row.price_count - 1)} изм.</small></button>;
      case 'price_per_sqm': return unitMoney(row.price_per_sqm);
      case 'source_updated_at': case 'last_seen_at': return date(row[key]);
      case 'url': return <a href={row.url} target="_blank" rel="noreferrer">№ {row.avito_id}</a>;
      default: return show(row[key]);
    }
  }
  const pages = Math.max(1, Math.ceil(data.total / data.page_size));
  const defaults = emptyFilters();
  const hasFilters = complexIds.length > 0 || Object.entries(filters).some(([key, value]) =>
    Array.isArray(value) ? value.length > 0 : value !== defaults[key]);
  return <>
    <div className="toolbar">
      <button className="start" onClick={() => start()} disabled={starting || active(run) || active(globalRun)}>{starting ? 'Запуск…' : active(run) ? 'Сбор выполняется…' : canResume ? 'Продолжить сбор' : 'Запустить сбор'}</button>
      {resumable.length > 1 && <label>Очередь для продолжения{' '}
        <select value={selectedResume.mode} onChange={event => setResumeMode(event.target.value)}
          disabled={starting || active(globalRun)}>
          {resumable.map(item => <option key={item.id} value={item.mode}>{modeNames[item.mode]} · №{item.id}</option>)}
        </select>
      </label>}
      <button onClick={() => start('new', 'full')} disabled={starting || active(run) || active(globalRun)}>Новый обход с начала</button>
      <button onClick={() => start('new', 'sample')} disabled={starting || active(run) || active(globalRun)}>Собрать 1000</button>
      <button onClick={() => start('new', 'refresh')} disabled={starting || active(run) || active(globalRun) || !city.has_data}>Обновить объявления</button>
      <span>Объявлений в базе: {data.total_all ?? data.total}</span>
      <button onClick={() => {
        refreshOptions();
        refresh().catch(e => { if (e.name !== 'AbortError') setError(e.message); });
      }}>Обновить таблицу</button>
      <CityPicker cities={cities} city={city} onSelect={onSelectCity} onRefresh={onRefreshCities} propertyKind={propertyKind} />
    </div>
    <h1>{propertyKind === 'house' ? 'Дома, дачи, коттеджи' : 'Квартиры'} — {city.name}</h1>
    {propertyKind === 'house' && <HouseRadius city={city} onRefresh={onRefreshCities} disabled={active(globalRun)} />}
    {active(globalRun) && globalRun.id !== run?.id && <p role="status">Сбор выполняется в другом разделе или городе. Дождитесь его завершения.</p>}
    {progress?.crawl_id && <p role="status">{progress.mode === 'refresh' ? 'Обновление' : 'Обход'} № {progress.crawl_id} ·
      {progress.radius_km != null && <> Радиус этой очереди: {progress.radius_km} км ·</>}
      {progress.range && progress.mode !== 'refresh' && <> Цена: {progress.range.label} · Страница продолжения: {progress.next_page} ·</>}
      Диапазонов пройдено: {progress.ranges_completed} · Осталось: {progress.ranges_remaining} ·
      Найдено уникальных: {progress.discovered} · Сохранено: {progress.saved} · Пропущено: {progress.skipped} ·
      В очереди: {progress.queued} · Ошибок: {progress.errors}. Прогресс сохраняется после каждого объявления.</p>}
    {progress?.ranges_limited > 0 && <p role="alert">Есть диапазоны с одной ценой, для которых выдача ограничена.
      Полнота сбора не подтверждена: {progress.ranges_limited}.</p>}
    {progress?.legacy_count > 0 && <p>В базе {progress.legacy_count} объявлений, собранных прежней версией.
      При продолжении страницы каталога будут просмотрены заново; сохранённые карточки открываться не будут.</p>}
    {progress?.sample && <div className="sample-progress" role="status">
      <strong>Выборка 1000</strong> · {progress.sample.phase === 'collect' ? 'Сбор объявлений' : 'Определение ценовых групп'}
      {progress.sample.population != null && <> · В выдаче: {progress.sample.population}</>}
      <div className="sample-groups">{progress.sample.groups.map(group=><span key={group.number}>
        {(group.number-1)*10}–{group.number*10}%: {group.saved}/{group.target}</span>)}</div>
    </div>}
    {run && <div className="status" role="status" aria-live="polite">
      <span><strong>{statuses[run.status]}</strong> · Запуск № {run.id} · {date(run.started_at || run.created_at)}</span>
      <span>Страниц: {run.pages} · Найдено: {run.found} · Сохранено: {run.saved} · Изменений цены: {run.price_changes} · В архив: {run.archived || 0} · Ошибок: {run.errors}</span>
      <span>{run.message}</span>
    </div>}
    {error && <p className="error" role="alert">{error}</p>}
    <div className="database-view-controls">
      <button aria-expanded={filtersVisible} aria-controls="database-filters"
        onClick={()=>setFiltersVisible(value=>!value)}>{filtersVisible ? 'Скрыть фильтры' : 'Показать фильтры'}{hasFilters ? ' · применены' : ''}</button>
      <button aria-expanded={fieldsVisible} aria-controls="database-fields" onClick={() => setFieldsVisible(value => !value)}>Настроить поля</button>
    </div>
    {fieldsVisible && <section id="database-fields" className="column-settings" aria-label="Настройка столбцов">
      <div className="column-settings-header"><h2>Отображаемые столбцы</h2>
        <span>Выбрано: {columnKeys.length} из {tableColumns.length}</span>
        <button onClick={() => setColumnKeys(allColumnKeys(propertyKind))}>Показать все</button></div>
      <div className="column-choices">{tableColumns.map(({key, label}) => <label key={key}>
        <input type="checkbox" checked={columnKeys.includes(key)} disabled={columnKeys.length === 1 && columnKeys.includes(key)}
          onChange={event => setColumnKeys(current => toggleColumn(current, key, event.target.checked, propertyKind))} />{label}
      </label>)}</div>
    </section>}
    <div id="database-filters" hidden={!filtersVisible}><Filters propertyKind={propertyKind} options={filterOptions} onApply={value => { setFilters(value); setPage(1); }}
      onReset={() => { setFilters(emptyFilters()); setComplexIds([]); setPage(1); }} /></div>
    {optionsError && <p role="alert">Не удалось загрузить списки фильтров: {optionsError}
      <button onClick={() => refreshOptions()}>Повторить загрузку списков</button></p>}
    <p className="results" role="status">Найдено: {data.total} из {data.total_all ?? data.total} объявлений.</p>
    <div className="table-scroll" role="region" aria-label="Таблица объявлений, доступна горизонтальная прокрутка" aria-busy={loading} tabIndex="0">
      <table className="listings" style={{minWidth: visibleColumns.reduce((sum, column) => sum + (column.width || 130), 0)}}><thead><tr>
        {visibleColumns.map(({key, label, sort: field}) => {
          const selected = field === sort.field;
          const nextDirection = selected && sort.direction === 'asc' ? 'по убыванию' : 'по возрастанию';
          const ariaSort = selected ? sort.direction === 'asc' ? 'ascending' : 'descending' : 'none';
          return <th key={key} scope="col" aria-sort={field ? ariaSort : undefined}>
            {key === 'complex_name' ? <ComplexFilter options={filterOptions} selected={complexIds}
              onChange={value => { setComplexIds(value); setPage(1); }} /> : field ? <button className="sort" onClick={() => changeSort(field)}
              aria-label={`${label}: сортировать ${nextDirection}`}>{label}
              <span aria-hidden="true">{selected ? sort.direction === 'asc' ? '↑' : '↓' : '↕'}</span></button> : label}
          </th>;
        })}
      </tr></thead><tbody>
        {data.items.map(row => <tr key={row.id}>{visibleColumns.map(column => <td key={column.key}
          className={column.key === 'price_per_sqm' ? 'unit-price' : column.key === 'address' ? 'address' : column.key === 'extras' ? 'extras' : undefined}>
          {cell(row, column)}</td>)}</tr>)}
        {!data.items.length && <tr><td colSpan={visibleColumns.length} className="empty">{loading ? 'Загрузка…' : error ? 'Нет данных для отображения.' : hasFilters ? 'По выбранным фильтрам объявления не найдены.' : 'В базе пока нет объявлений. Нажмите «Запустить сбор».'}</td></tr>}
      </tbody></table>
    </div>
    <div className="pagination"><button disabled={page <= 1} onClick={() => setPage(p => p - 1)}>Назад</button>
      <span>Страница {page} из {pages} · по 100 объявлений</span><button disabled={page >= pages} onClick={() => setPage(p => p + 1)}>Далее</button></div>
    <p className="note">Прочерк означает, что Авито не передал значение. Дата обновления объявления и дата проверки хранятся отдельно.</p>
    {history && <History listing={history} onClose={() => setHistory(null)} />}
  </>;
}
