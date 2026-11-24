import {test} from 'node:test';
import assert from 'node:assert/strict';
import {TABLE_COLUMNS, allColumnKeys, decodeColumnSettings, toggleColumn} from '../src/table-columns.js';

test('all fields have unique stable keys and include the four building fields', () => {
  const keys = allColumnKeys();
  assert.equal(new Set(keys).size, keys.length);
  for (const key of ['house_type', 'construction_year', 'passenger_elevator', 'parking']) assert.ok(keys.includes(key));
  assert.equal(TABLE_COLUMNS[keys.indexOf('price') + 1].key, 'price_per_sqm');
});

test('missing or damaged preferences restore all columns', () => {
  for (const value of [null, '{broken', 'null', '{}', '[]', '["obsolete"]']) {
    assert.deepEqual(decodeColumnSettings(value), allColumnKeys());
  }
});

test('preferences survive serialization and discard duplicate and unknown keys in table order', () => {
  const selected = ['parking', 'address', 'price', 'address', 'obsolete'];
  assert.deepEqual(decodeColumnSettings(JSON.stringify(selected)), ['address', 'parking', 'price']);
  assert.deepEqual(decodeColumnSettings(JSON.stringify(['construction_year'])), ['construction_year']);
});

test('the previous full set gains the archive status column while custom selections are preserved', () => {
  const previous = allColumnKeys().filter(key => key !== 'listing_status');
  assert.deepEqual(decodeColumnSettings(JSON.stringify(previous)), allColumnKeys());
  assert.deepEqual(decodeColumnSettings(JSON.stringify(['address', 'price'])), ['address', 'price']);
});

test('previous complete tables gain offer classification and district, while custom fields remain selected', () => {
  for (const kind of ['apartment', 'house']) {
    const previous = allColumnKeys(kind).filter(key => !['offer_category', 'district'].includes(key));
    assert.deepEqual(decodeColumnSettings(JSON.stringify(previous), kind), allColumnKeys(kind));
    assert.deepEqual(decodeColumnSettings('["address","price"]', kind), ['address','price']);
  }
});

test('toggling fields preserves order, prevents an empty table, and can restore hidden columns', () => {
  assert.deepEqual(toggleColumn(['address', 'price'], 'address', false), ['price']);
  assert.deepEqual(toggleColumn(['price'], 'price', false), ['price']);
  assert.deepEqual(toggleColumn(['price'], 'construction_year', true), ['construction_year', 'price']);
  assert.deepEqual(toggleColumn(['address'], 'invalid', true), ['address']);
});

test('house fields and preferences are independent of apartment fields', async () => {
  const {HOUSE_COLUMNS, columnsFor, columnSettingsKey} = await import('../src/table-columns.js');
  const keys = allColumnKeys('house');
  assert.equal(new Set(keys).size, keys.length);
  for (const key of ['object_type','readiness','land_area','distance_to_center_km','floors_total','land_category','electricity','water_supply','gas','sewerage','recreation','wall_material','transport_access','infrastructure']) assert.ok(keys.includes(key));
  assert.equal(HOUSE_COLUMNS[keys.indexOf('price') + 1].key, 'price_per_sqm');
  assert.ok(!keys.includes('passenger_elevator'));
  assert.notEqual(columnSettingsKey('house'),columnSettingsKey('apartment'));
  assert.deepEqual(decodeColumnSettings('["land_area","price"]','house'),['land_area','price']);
  assert.deepEqual(toggleColumn(['price'],'land_area',true,'house'),['land_area','price']);
  assert.equal(columnsFor('house'), HOUSE_COLUMNS);
});
