import {test} from 'node:test';
import assert from 'node:assert/strict';
import {heatColor, heatDomain, heatRadius, heatRaster} from '../src/price-heat.js';

const point = (price, count=1, x=42, y=42) => ({price_mean: price, priced_count: count, x, y});
const domain = {low: 2_000_000, high: 10_000_000};
const pixel = (frame, x=10, y=10) => [...frame.pixels.slice((y*frame.width+x)*4, (y*frame.width+x)*4+4)];

test('low prices are green and high prices are red, with a smooth middle', () => {
  assert.deepEqual(heatColor(2_000_000, domain), [26,152,80]);
  assert.deepEqual(heatColor(10_000_000, domain), [215,48,39]);
  assert.deepEqual(heatColor(6_000_000, domain), [254,224,139]);
  assert.deepEqual(heatColor(400_000_000, domain), heatColor(domain.high, domain));
  const a=heatColor(5_999_999,domain), b=heatColor(6_000_001,domain);
  assert.ok(a.every((v,i) => Math.abs(v-b[i]) <= 1));
});

test('extra density of equally priced listings never changes price color', () => {
  const sparse=heatRaster([point(3_000_000)],84,84,32,domain);
  const dense=heatRaster(Array.from({length:100},()=>point(3_000_000)),84,84,32,domain);
  assert.deepEqual(pixel(sparse).slice(0,3), pixel(dense).slice(0,3));
  assert.ok(pixel(dense)[3] >= pixel(sparse)[3]);
});

test('overlapping houses blend actual prices using the number of priced apartments', () => {
  const frame=heatRaster([point(3_000_000,3),point(9_000_000,1)],84,84,32,domain);
  assert.deepEqual(pixel(frame).slice(0,3), heatColor(4_500_000,domain));
});

test('empty areas stay transparent and edges fade continuously to zero', () => {
  const frame=heatRaster([point(3_000_000)],84,84,32,domain);
  assert.equal(pixel(frame,0,0)[3],0);
  assert.ok(pixel(frame,10,10)[3] > pixel(frame,15,10)[3]);
  assert.ok(pixel(frame,15,10)[3] > pixel(frame,17,10)[3]);
  assert.equal(pixel(frame,19,10)[3],0);
  const separated=heatRaster([point(2_000_000,1,22,22),point(10_000_000,1,182,22)],204,44,24,domain);
  assert.equal(pixel(separated,25,5)[3],0);
});

test('points just outside the viewport contribute smoothly at its edge', () => {
  const frame=heatRaster([point(9_000_000,1,-5,22)],84,44,32,domain);
  assert.ok(pixel(frame,0,5)[3] > 0);
  assert.equal(pixel(frame,20,5)[3],0);
});

test('missing prices and empty selections produce no colored pixels', () => {
  const points=[point(null),point(NaN),point(2_000_000,0),point(-1)];
  assert.equal(heatDomain(points),null);
  assert.ok(heatRaster(points,84,84,32,domain).pixels.every(channel=>channel===0));
  assert.ok(heatRaster([],84,84,32,null).pixels.every(channel=>channel===0));
});

test('city-wide scale handles uniform prices and isolated extreme outliers', () => {
  assert.deepEqual(heatDomain([point(5_000_000),point(5_000_000)]), {low:5_000_000,high:5_000_000});
  assert.deepEqual(heatColor(5_000_000,{low:5_000_000,high:5_000_000}), [254,224,139]);
  const prices=Array.from({length:100},(_,i)=>point((i+1)*100_000));
  const scale=heatDomain([...prices,point(500_000_000)]);
  assert.ok(scale.high < 10_000_000 && scale.low > 100_000);
  const rare=heatDomain([...Array.from({length:100},()=>point(5_000_000)),point(500_000_000)]);
  assert.equal(rare.high,500_000_000);
});

test('radius stays finite and bounded for city and street zoom levels', () => {
  for(const zoom of [3,10,12,14,16,19]) for(const latitude of [0,52.3,70]) {
    const radius=heatRadius(zoom,latitude);
    assert.ok(Number.isFinite(radius) && radius>=32 && radius<=100);
  }
});
