import React, {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import {api, filterParams} from './api.js';
import {Filters, emptyFilters} from './filters.jsx';
import {CityPicker} from './cities.jsx';
import {clusterPoints, priceLabel} from './map-points.js';
import {HEAT_COLORS, heatDomain} from './price-heat.js';
import {PriceHeatLayer} from './price-heat-layer.js';
import {money} from './format.js';
import {useLatestRequest} from './latest-request.js';


function MapCanvas({data, active, selection, onSelect, picking, candidate, onPick, fitVersion, heatEnabled, propertyKind}) {
  const host = useRef(null), map = useRef(null), layer = useRef(null), tiles = useRef(null);
  const latest = useRef({data, onSelect, picking, onPick});
  latest.current = {data, onSelect, picking, onPick};
  const [tileError, setTileError] = useState(false);
  const heat = useRef(null);
  const domain = useMemo(() => heatDomain(data.points), [data.points]);
  const draw = useCallback(() => {
    if (!map.current || !latest.current.data) return;
    const instance = map.current, {data, onSelect} = latest.current;
    layer.current.clearLayers();
    const zoom = instance.getZoom();
    const visible = data.points.filter(p => instance.getBounds().pad(.15).contains([p.latitude, p.longitude]));
    for (const group of clusterPoints(visible, zoom)) {
      const clustered = group.members.length > 1;
      const label = zoom >= 14 ? priceLabel(group.price_min, group.price_max) : String(group.count);
      const element = document.createElement('span');
      element.textContent = label;
      const icon = L.divIcon({className: `apartment-pin ${zoom >= 14 ? 'pin-price' : 'pin-dot'}${clustered ? ' pin-cluster' : ''}`,
        html: element, iconSize: zoom >= 14 ? null : [25, 25], iconAnchor: zoom >= 14 ? [0, 14] : [12, 12]});
      const title = `${group.members.length === 1 ? group.members[0].address : `${group.members.length} адресов`} · ${group.count} объявлений · ${priceLabel(group.price_min, group.price_max)}`;
      const marker = L.marker([group.latitude, group.longitude], {icon, title, keyboard: true, riseOnHover: true}).addTo(layer.current);
      marker.on('click', () => {
        if (latest.current.picking) return;
        if (clustered && zoom < 18) {
          instance.fitBounds(L.latLngBounds(group.members.map(p => [p.latitude, p.longitude])), {maxZoom: Math.min(18, zoom+2), padding: [70, 70]});
        } else onSelect(group);
      });
    }
  }, []);
  useEffect(() => {
    const instance = L.map(host.current, {zoomControl: false, maxZoom: 19, minZoom: 3, scrollWheelZoom: true});
    L.control.zoom({zoomInTitle: 'Приблизить', zoomOutTitle: 'Отдалить'}).addTo(instance);
    map.current = instance;
    layer.current = L.layerGroup().addTo(instance);
    heat.current = new PriceHeatLayer();
    instance.setView([61, 90], 4);
    instance.on('moveend zoomend', draw);
    instance.on('click', event => { if (latest.current.picking) latest.current.onPick(event.latlng); });
    const observer = new ResizeObserver(() => { instance.invalidateSize(); draw(); });
    observer.observe(host.current);
    return () => { observer.disconnect(); instance.remove(); map.current = null; };
  }, [draw]);
  useEffect(() => {
    if (!map.current || !heat.current) return;
    if (heatEnabled && active) {
      heat.current.setData(data.points);
      if (!map.current.hasLayer(heat.current)) heat.current.addTo(map.current);
    } else heat.current.remove();
  }, [heatEnabled, active, data.points]);
  useEffect(() => {
    if (!data || !map.current) return;
    tiles.current?.remove();
    setTileError(false);
    tiles.current = L.tileLayer(data.tiles.url, {attribution: data.tiles.attribution, maxZoom: 19,
      updateWhenIdle: true, keepBuffer: 1}).addTo(map.current);
    tiles.current.on('tileerror', () => setTileError(true));
    return () => tiles.current?.remove();
  }, [data?.tiles.url, data?.tiles.attribution]);
  useEffect(() => {
    if (!map.current || !data) return;
    map.current.setView(data.city.center, 12, {animate: false});
  }, [data?.city.id]);
  useEffect(() => {
    if (!map.current || !data.city.radius_km) return;
    const circle = L.circle(data.city.center, {radius:data.city.radius_km*1000,color:'#666',weight:1,dashArray:'5 5',fill:false}).addTo(map.current);
    map.current.fitBounds(circle.getBounds(), {padding:[20,20],animate:false});
    return ()=>circle.remove();
  }, [data.city.id, data.city.radius_km]);
  useEffect(() => {
    if (data?.points.length && map.current) map.current.fitBounds(data.points.map(p => [p.latitude, p.longitude]), {padding: [40, 40], maxZoom: 15});
  }, [fitVersion]);
  useEffect(() => { if (active && map.current) { map.current.invalidateSize(); draw(); } }, [active, data, selection, draw]);
  useEffect(() => {
    if (!candidate || !map.current) return;
    const pin = L.circleMarker([candidate.lat, candidate.lng], {radius: 9, color: '#000', weight: 3, fillColor: '#fff', fillOpacity: 1}).addTo(map.current);
    return () => pin.remove();
  }, [candidate]);
  return <div className={`map-surface${picking ? ' map-picking' : ''}`}>
    <div ref={host} className="apartment-map" aria-label={propertyKind === "house" ? "Карта домов и окрестностей города" : "Карта квартир выбранного города"} />
    {heatEnabled && <div className="heat-legend" role="group" aria-label="Шкала цен тепловой карты">
      <strong>Средняя цена {propertyKind === "house" ? "дома" : "квартиры"} поблизости</strong>
      {domain ? domain.low === domain.high ? <p className="heat-single-price"><span style={{background: HEAT_COLORS[2]}} />{priceLabel(domain.low, domain.high)}</p> : <>
        <div className="heat-gradient" style={{background: `linear-gradient(to right, ${HEAT_COLORS.join(',')})`}} />
        <div className="heat-labels"><span>до {priceLabel(domain.low, domain.low)}</span><span>от {priceLabel(domain.high, domain.high)}</span></div>
        <div className="heat-directions"><span>Дешевле</span><span>Дороже</span></div>
      </> : <p>Нет объявлений с ценой и координатами.</p>}
    </div>}
    {tileError && <p className="map-tile-error" role="alert">Не удалось загрузить подложку карты. Проверьте подключение к интернету.</p>}
  </div>;
}

function ApartmentList({selection, city, filters, onClose, propertyKind}) {
  const [data, setData] = useState(null), [page, setPage] = useState(1), [error, setError] = useState('');
  useEffect(() => { setPage(1); setData(null); }, [selection]);
  useEffect(() => {
    const controller = new AbortController();
    const body = {...Object.fromEntries(Object.entries(filters).filter(([, value]) => value !== '')),
      city_id: city.id, property_kind: propertyKind, page, address_id: selection.address_ids};
    api('/map/listings', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body), signal: controller.signal}).then(result => {
      if (!controller.signal.aborted) {setData(result); setError('');}
    })
      .catch(e => {if (e.name !== 'AbortError') setError(e.message);});
    return () => controller.abort();
  }, [selection, city.id, filters, page, propertyKind]);
  return <aside className="map-apartments" aria-label="Объявления в выбранной точке">
    <div className="map-list-header"><h2>{selection.members.length === 1 ? selection.members[0].address : 'Объявления в точке'}</h2>
      <button onClick={onClose} aria-label="Закрыть список объявлений">×</button></div>
    <p>{priceLabel(selection.price_min, selection.price_max)} · {selection.count} объявлений</p>
    {error && <p role="alert">{error}</p>}
    {!data && !error && <p role="status">Загрузка…</p>}
    {data?.items.map(row => <article key={row.id} className="map-apartment">
      <strong>{money(row.price)}</strong><p>{row.title}</p><p>{row.address}</p>
      <a href={row.url} target="_blank" rel="noreferrer">Открыть объявление № {row.avito_id}</a>
    </article>)}
    {data && data.total > data.page_size && <div className="pagination">
      <button disabled={page === 1} onClick={() => setPage(p => p-1)}>Назад</button>
      <span>{page} / {Math.ceil(data.total/data.page_size)}</span>
      <button disabled={page*data.page_size >= data.total} onClick={() => setPage(p => p+1)}>Далее</button>
    </div>}
  </aside>;
}

export function ApartmentsMap({city, cities, onSelectCity, onRefreshCities, active, propertyKind}) {
  const [data, setData] = useState(null), [filters, setFilters] = useState(emptyFilters), [options, setOptions] = useState(null);
  const [error, setError] = useState(''), [optionsError, setOptionsError] = useState(''), [loading, setLoading] = useState(false);
  const [selection, setSelection] = useState(null), [picking, setPicking] = useState(null), [candidate, setCandidate] = useState(null);
  const [missingOpen, setMissingOpen] = useState(false), [search, setSearch] = useState(''), [filtersOpen, setFiltersOpen] = useState(true);
  const [fitVersion, setFitVersion] = useState(0);
  const [heatEnabled, setHeatEnabled] = useState(false);
  const [beginRequest, abortRequest] = useLatestRequest();
  const displayed = useRef(null);
  const refresh = useCallback(async () => {
    const signal = beginRequest();
    setLoading(true);
    try {
      const result = await api(`/map?${filterParams({...filters, city_id: city.id, property_kind:propertyKind})}`, {signal});
      if (!signal.aborted) {displayed.current = result; setData(result); setError('');}
    } catch(e) {if (!signal.aborted) setError(e.message);}
    finally {if (!signal.aborted) setLoading(false);}
  }, [beginRequest, filters, city.id, propertyKind]);
  const latestRefresh = useRef(refresh);
  latestRefresh.current = refresh;
  async function prepare() {
    try {await api(`/map/prepare?city_id=${city.id}`, {method: 'POST'}); await latestRefresh.current();}
    catch(e) {setError(e.message);}
  }
  useEffect(() => {
    const controller = new AbortController();
    api(`/listings/filter-options?city_id=${city.id}&property_kind=${propertyKind}&listing_status=all`, {signal: controller.signal}).then(setOptions)
      .catch(e => {if (e.name !== 'AbortError') setOptionsError(e.message);});
    return () => controller.abort();
  }, [city.id, propertyKind]);
  useEffect(() => {
    if (!active) return;
    refresh();
    const controller = new AbortController();
    let checking = false;
    async function poll() {
      if (checking) return;
      checking = true;
      try {
        const latest = await api(`/map/status?city_id=${city.id}&property_kind=${propertyKind}`,
          {signal: controller.signal});
        if (controller.signal.aborted) return;
        const old = displayed.current;
        if (!old || old.data_version !== latest.data_version ||
            old.import?.finished_at !== latest.import?.finished_at) {
          await refresh();
        } else if (old.import?.status !== latest.import?.status) {
          displayed.current = {...old, import: latest.import};
          setData(displayed.current);
        }
      } catch (e) {
        if (!controller.signal.aborted) setError(e.message);
      } finally {
        checking = false;
      }
    }
    const timer = setInterval(poll, 10000);
    return () => {clearInterval(timer); controller.abort(); abortRequest();};
  }, [active, refresh, abortRequest, city.id, propertyKind]);
  useEffect(() => { if (active && data && !data.import && data.located < data.total) prepare(); }, [active, !!data, data?.import?.status, data?.located, data?.total]);
  useEffect(() => {setSelection(null); setPicking(null); setCandidate(null);}, [filters]);
  async function savePoint() {
    try {
      await api(`/map/addresses/${picking.id}?city_id=${city.id}`, {method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({latitude: candidate.lat, longitude: candidate.lng})});
      setPicking(null); setCandidate(null); await latestRefresh.current();
    } catch(e) {setError(e.message);}
  }
  const importBusy = ['queued', 'running'].includes(data?.import?.status);
  return <>
    <div className="toolbar"><h1>Карта · {propertyKind === "house" ? "Дома" : "Квартиры"} — {city.name}</h1>
      <CityPicker cities={cities} city={city} onSelect={onSelectCity} onRefresh={onRefreshCities} propertyKind={propertyKind} /></div>
    <button className="filters-toggle" aria-controls="map-filters" aria-expanded={filtersOpen}
      onClick={() => setFiltersOpen(v => !v)}>{filtersOpen ? 'Скрыть фильтры' : 'Показать фильтры'}</button>
    <div id="map-filters" hidden={!filtersOpen}><Filters propertyKind={propertyKind} label="Фильтры карты" options={options} complexes
      onApply={setFilters} onReset={() => setFilters(emptyFilters())} /></div>
    {optionsError && <p role="alert">Не удалось загрузить фильтры: {optionsError}</p>}
    {error && <p role="alert">{error} <button onClick={refresh}>Повторить</button></p>}
    <div className="map-toolbar" role="status"><span>{loading && !data ? 'Загрузка…' : `На карте: ${data?.located ?? 0} из ${data?.total ?? 0} объявлений · ${data?.points.length ?? 0} адресов`}</span>
      <label className="heat-toggle"><input type="checkbox" checked={heatEnabled} onChange={event => setHeatEnabled(event.target.checked)} />Тепловая карта</label>
      <button onClick={refresh}>Обновить карту</button><button onClick={() => setFitVersion(v => v+1)} disabled={!data?.points.length}>Показать все точки</button>
      {!!data?.missing.length && <button aria-expanded={missingOpen} onClick={() => setMissingOpen(v => !v)}>Без координат: {data.missing.reduce((sum, item) => sum+item.count, 0)}</button>}
    </div>
    {importBusy && <p role="status">Определяются координаты сохранённых адресов. Точки будут появляться автоматически.</p>}
    {data?.import?.status === 'failed' && <p role="alert">{data.import.error} <button onClick={prepare}>Повторить подготовку карты</button></p>}
    {picking && <div className="map-pick-bar" role="status"><strong>{picking.address}</strong> · Нажмите на нужный дом на карте.
      {candidate && <><span> {candidate.lat.toFixed(6)}, {candidate.lng.toFixed(6)}</span><button onClick={savePoint}>Сохранить точку</button></>}
      <button onClick={() => {setPicking(null); setCandidate(null);}}>Отмена</button></div>}
    {missingOpen && <div className="map-missing"><label>Найти адрес без координат <input type="search" value={search} onChange={e => setSearch(e.target.value)} /></label>
      <div>{data?.missing.filter(item => item.address.toLocaleLowerCase('ru').includes(search.toLocaleLowerCase('ru'))).map(item => <p key={item.id}>
        <span>{item.address} · {item.count} объявлений</span><button onClick={() => {setPicking(item); setCandidate(null); setSelection(null); setMissingOpen(false);}}>Указать на карте</button>
      </p>)}</div></div>}
    {propertyKind === "house" && <p className="note">Радиус сбора от центра города: {city.house_radius_km} км.</p>}
    {data && <div className={`map-layout${selection ? ' map-with-list' : ''}`}>
      <MapCanvas propertyKind={propertyKind} data={data} active={active} selection={selection} onSelect={setSelection} picking={picking} candidate={candidate} onPick={setCandidate} fitVersion={fitVersion} heatEnabled={heatEnabled} />
      {selection && <ApartmentList propertyKind={propertyKind} key={selection.address_ids.join(',')} selection={selection} city={city} filters={filters} onClose={() => setSelection(null)} />}
    </div>}
    <p className="note">Приблизьте карту, чтобы увидеть цены. На одной точке показан диапазон цен объявлений по этому адресу.</p>
  </>;
}
