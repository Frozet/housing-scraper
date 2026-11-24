import React, {lazy, Suspense, useCallback, useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Database} from './database.jsx';
import {api} from './api.js';
import './style.css';

const Analytics = lazy(() => import('./analytics.jsx').then(module => ({default: module.Analytics})));
const ApartmentsMap = lazy(() => import('./maps.jsx').then(module => ({default: module.ApartmentsMap})));

function App() {
  const [tab, setTab] = useState('database');
  const [propertyKind, setPropertyKind] = useState(() => { try { return localStorage.getItem('selected-property-kind') === 'house' ? 'house' : 'apartment'; } catch { return 'apartment'; } });
  const [analyticsOpened, setAnalyticsOpened] = useState(false);
  const [mapOpened, setMapOpened] = useState(false);
  const [cities, setCities] = useState([]), [cityError, setCityError] = useState('');
  const cityRequest = useRef(null);
  const [cityId, setCityId] = useState(()=>{ try { return Number(localStorage.getItem('selected-city')) || null; } catch { return null; } });
  const refreshCities = useCallback(async()=>{
    cityRequest.current?.abort(); const controller = new AbortController(); cityRequest.current = controller;
    try {
      const data=await api(`/cities?property_kind=${propertyKind}`, {signal:controller.signal});
      if (controller.signal.aborted) return; setCities(data.items); setCityError('');
      setCityId(current=>data.items.some(item=>item.id===current) ? current : data.default_city_id);
    } catch(e) { if (e.name !== 'AbortError') setCityError(e.message); }
  }, [propertyKind]);
  useEffect(()=>{ refreshCities(); return ()=>cityRequest.current?.abort(); },[refreshCities]);
  function chooseCity(id) { setCityId(id); try { localStorage.setItem('selected-city',String(id)); } catch {} }
  const city = cities.find(item=>item.id===cityId);
  const tabs = [{id: 'database', label: 'База данных'}, {id: 'analytics', label: 'Аналитика'}, {id: 'map', label: 'Карта'}];
  function selectTab(id, focus = false) {
    if (id === 'analytics') setAnalyticsOpened(true);
    if (id === 'map') setMapOpened(true);
    setTab(id);
    if (focus) document.getElementById(`${id}-tab`)?.focus();
  }
  return <main>
    <div className="app-nav">
      <div className="tabs" role="tablist" aria-label="Разделы">
        {tabs.map(({id, label}, index) => <button key={id} id={`${id}-tab`} role="tab" type="button"
          aria-selected={tab === id} aria-controls={`${id}-panel`} tabIndex={tab === id ? 0 : -1}
          onClick={() => selectTab(id)} onKeyDown={event => {
            const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 :
              event.key === 'ArrowRight' ? (index + 1) % tabs.length :
              event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length : null;
            if (next !== null) { event.preventDefault(); selectTab(tabs[next].id, true); }
          }}>{label}</button>)}
      </div>
      <div className="tabs property-tabs" role="tablist" aria-label="Рынок недвижимости">
        {[['apartment','Квартиры'],['house','Дома']].map(([kind,label]) => <button key={kind} type="button" role="tab"
          id={`property-${kind}`} aria-selected={propertyKind === kind} tabIndex={propertyKind === kind ? 0 : -1}
          onKeyDown={event => { if (['ArrowLeft','ArrowRight','Home','End'].includes(event.key)) {
            event.preventDefault(); const next = event.key === 'Home' ? 'apartment' : event.key === 'End' ? 'house' : propertyKind === 'house' ? 'apartment' : 'house';
            setPropertyKind(next); try { localStorage.setItem('selected-property-kind',next); } catch {} document.getElementById(`property-${next}`)?.focus();
          } }} onClick={() => {setPropertyKind(kind); try {localStorage.setItem('selected-property-kind',kind);} catch {}}}>{label}</button>)}
      </div>
    </div>
    {cityError && <p role="alert">Не удалось загрузить города: {cityError} <button onClick={refreshCities}>Повторить</button></p>}
    {!city && !cityError && <p role="status">Загрузка городов…</p>}
    <section id="database-panel" role="tabpanel" aria-labelledby="database-tab" hidden={tab !== 'database'}>{city && <Database key={`${city.id}:${propertyKind}`} propertyKind={propertyKind} city={city} cities={cities} onSelectCity={chooseCity} onRefreshCities={refreshCities} visible={tab === 'database'} />}</section>
    <section id="analytics-panel" role="tabpanel" aria-labelledby="analytics-tab" hidden={tab !== 'analytics'}>
      {analyticsOpened && city && <Suspense fallback={<p role="status">Загрузка аналитики…</p>}>
        <Analytics key={`${city.id}:${propertyKind}`} propertyKind={propertyKind} city={city} active={tab === 'analytics'} />
      </Suspense>}
    </section>
    <section id="map-panel" role="tabpanel" aria-labelledby="map-tab" hidden={tab !== 'map'}>
      {mapOpened && city && <Suspense fallback={<p role="status">Загрузка карты…</p>}>
        <ApartmentsMap key={`${city.id}:${propertyKind}`} propertyKind={propertyKind} city={city} cities={cities} onSelectCity={chooseCity}
          onRefreshCities={refreshCities} active={tab === 'map'} />
      </Suspense>}
    </section>
  </main>;
}

createRoot(document.getElementById('root')).render(<App />);
