import React, { useEffect, useRef, useState } from 'react';
import { api } from './api.js';

function DatabaseMark({count}) {
  return <svg className="database-mark" viewBox="0 0 16 16" width="14" height="14" role="img" aria-label={`Есть база: ${count} объявлений`}>
    <ellipse cx="8" cy="3.5" rx="5.5" ry="2" fill="none" stroke="currentColor" />
    <path d="M2.5 3.5v8.5c0 2.7 11 2.7 11 0V3.5M2.5 8c0 2.7 11 2.7 11 0" fill="none" stroke="currentColor" />
  </svg>;
}
const normalized = text => text.toLocaleLowerCase('ru').replaceAll('ё','е').replaceAll('-',' ');

export function CityPicker({cities, city, onSelect, onRefresh, propertyKind = "apartment"}) {
  const root = useRef(null), searchInput = useRef(null);
  const [open, setOpen] = useState(false), [search, setSearch] = useState('');
  const [url, setUrl] = useState(city.catalog_url), [error, setError] = useState(''), [saving,setSaving] = useState(false);
  useEffect(() => setUrl(city.catalog_url), [city.catalog_url]);
  useEffect(() => {
    if (!open) return;
    searchInput.current?.focus();
    const outside = event => { if (!root.current?.contains(event.target)) setOpen(false); };
    const escape = event => { if (event.key === 'Escape') { setOpen(false); root.current?.querySelector('button').focus(); } };
    document.addEventListener('pointerdown',outside);
    document.addEventListener('keydown',escape);
    return () => { document.removeEventListener('pointerdown',outside); document.removeEventListener('keydown',escape); };
  }, [open]);
  const visible = cities.filter(item => normalized(`${item.name} ${item.region}`).includes(normalized(search)));
  async function saveUrl(event) {
    event.preventDefault(); setError(''); setSaving(true);
    try { await api(`/cities/${city.id}/catalog?property_kind=${propertyKind}`, {method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({catalog_url:url})}); await onRefresh(); }
    catch(e) { setError(e.message); }
    finally { setSaving(false); }
  }
  return <div className="city-picker" ref={root}>
    <button type="button" aria-expanded={open} aria-controls="city-picker-panel" aria-label={`Выбрать город: ${city.name}`}
      onClick={() => { setOpen(value=>!value); if (!open) { setSearch(''); onRefresh(); } }}>
      {city.has_data && <DatabaseMark count={city.listing_count} />} {city.name} ▾
    </button>
    {open && <div className="city-popover" id="city-picker-panel" role="dialog" aria-label="Выбор города">
      <label>Поиск города<input ref={searchInput} type="search" placeholder="Название города или регион" value={search}
        onChange={event=>setSearch(event.target.value)} aria-label="Найти город" /></label>
      <div className="city-options">
        {visible.map(item=><button type="button" key={item.id} aria-pressed={city.id===item.id}
          onClick={()=>{onSelect(item.id);setOpen(false);}}>
          <span>{item.has_data && <DatabaseMark count={item.listing_count} />} {item.name}</span>
          <small>{item.region}{item.has_data ? ` · ${item.listing_count} объявлений` : ''}</small>
        </button>)}
        {!visible.length && <p>Город не найден.</p>}
      </div>
      <details className="city-address"><summary>Адрес каталога Авито для {city.name}</summary>
        <form onSubmit={saveUrl}><label>Ссылка на каталог<input type="url" required value={url} onChange={e=>setUrl(e.target.value)} /></label>
          <button disabled={saving} type="submit">{saving?'Сохранение…':'Сохранить адрес'}</button></form>
        {error && <p role="alert">{error}</p>}
      </details>
      <button type="button" onClick={()=>setOpen(false)}>Закрыть список</button>
    </div>}
  </div>;
}
