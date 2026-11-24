import {test} from 'node:test';
import assert from 'node:assert/strict';
import {clusterPoints, priceLabel} from '../src/map-points.js';

test('single prices and ranges use readable units', () => {
  assert.equal(priceLabel(3_500_000, 3_500_000), '3,5 млн ₽');
  assert.equal(priceLabel(3_500_000, 7_000_000), '3,5–7 млн ₽');
  assert.equal(priceLabel(null, null), 'Без цены');
});

const points = [
  {id:1, latitude:52.28, longitude:104.3, count:3, price_min:3_000_000, price_max:5_000_000, address_ids:[1]},
  {id:2, latitude:52.2805, longitude:104.3005, count:2, price_min:6_000_000, price_max:8_000_000, address_ids:[2]},
];
test('zooming separates houses without losing listings or price bounds', () => {
  const far = clusterPoints(points, 10);
  assert.equal(far.length,1);
  assert.equal(far[0].count,5);
  assert.equal(far[0].price_min,3_000_000);
  assert.equal(far[0].price_max,8_000_000);
  assert.deepEqual(far[0].address_ids,[1,2]);
  assert.equal(clusterPoints(points,18).length,2);
});
test('coincident points remain selectable at maximum zoom', () => {
  const groups = clusterPoints([points[0],{...points[1],latitude:52.28,longitude:104.3}],19);
  assert.equal(groups.length,1);
  assert.equal(groups[0].count,5);
  assert.deepEqual(groups[0].address_ids,[1,2]);
  assert.deepEqual(clusterPoints([],12),[]);
});
