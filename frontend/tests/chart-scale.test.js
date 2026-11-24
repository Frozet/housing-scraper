import assert from 'node:assert/strict';
import test from 'node:test';
import { chartAxis } from '../src/chart-scale.js';

const data = Array.from({length: 100}, (_, index) => 20 + index * 0.6).concat(600);

test('long tails use a gradual scale while ordinary values keep linear distances', () => {
  const axis = chartAxis(data, 1800, 12);
  assert.equal(axis.compressed, true);
  assert.equal(axis.position(0), 0);
  assert.equal(axis.position(axis.max), 1);
  assert.equal(axis.position(20) * 2, axis.position(40));
  assert.ok(axis.position(axis.mainMax) > 0.5 && axis.position(axis.mainMax) < 0.7);
  assert.ok(axis.position(60) > 0.4);
  const tail = [80, 120, 200, 400, 600].map(axis.position);
  assert.ok(tail.every((value, index) => index === 0 || value - tail[index - 1] > 0.04));
});

test('all values remain in bounds and in the same order, including extremes', () => {
  const axis = chartAxis(data, 1800, 12);
  const values = [...data, axis.mainMax, axis.max].sort((a, b) => a - b);
  const positions = values.map(axis.position);
  assert.ok(positions.every(value => Number.isFinite(value) && value >= 0 && value <= 1));
  assert.ok(positions.every((value, index) => index === 0 || value >= positions[index - 1]));
  assert.ok(axis.max > Math.max(...data));
});

test('a small sample with two very different outliers has no collapsed or reversed points', () => {
  const values = [...Array(20).fill(1), 100, 1000];
  const axis = chartAxis(values, 900, 10);
  assert.equal(axis.compressed, true);
  assert.ok(axis.position(1) < axis.position(100));
  assert.ok(axis.position(1000) - axis.position(100) > 0.3);
});

test('linear mode preserves distances even when there are outliers', () => {
  const axis = chartAxis(data, 900, 10, 'linear');
  assert.equal(axis.compressed, false);
  assert.equal(axis.position(20) * 2, axis.position(40));
  assert.equal(axis.mainMax, axis.max);
});

test('ordinary data and small samples do not get a nonlinear scale', () => {
  assert.equal(chartAxis([10, 20, 30, 600], 900, 10).compressed, false);
  assert.equal(chartAxis(Array.from({length: 100}, (_, i) => i + 10), 900, 10).compressed, false);
});

for (const [name, values] of [['empty', []], ['zero', [0]], ['singleton', [7]], ['constant', Array(50).fill(10)],
  ['fractions', [0.01, 0.02, 0.03]], ['large', data.map(value => value * 1_000_000)]]) {
  test(`${name}: finite domain, ordered ticks and no division by zero`, () => {
    const axis = chartAxis(values, 900, 10);
    assert.ok(Number.isFinite(axis.max) && axis.max > 0);
    assert.ok(Number.isFinite(axis.mainMax) && axis.mainMax > 0);
    assert.equal(axis.ticks[0], 0);
    assert.equal(axis.ticks.at(-1), axis.max);
    assert.ok(axis.ticks.every((value, index) => Number.isFinite(value) && (index === 0 || value > axis.ticks[index - 1])));
    assert.ok(values.every(value => Number.isFinite(axis.position(value))));
  });
}

test('tick density doubles in a wide chart and adapts to a narrow chart', () => {
  const wide = chartAxis([0, 500], 1800, 10, 'linear');
  assert.equal(wide.ticks.length, 12);
  const narrow = chartAxis([0, 500], 250, 3, 'linear');
  assert.ok(narrow.ticks.length < wide.ticks.length);
});

test('nonlinear divisions are equally spaced and their value increments grow gradually', () => {
  for (const pixels of [200, 700, 1900]) {
    const axis = chartAxis(data, pixels, 10);
    const gaps = axis.ticks.slice(1).map((value, index) => value - axis.ticks[index]);
    axis.ticks.forEach((value, index) => assert.ok(Math.abs(axis.position(value) - index / gaps.length) < 1e-10));
    assert.ok(gaps.every((gap, index) => index === 0 || gap >= gaps[index - 1] - 1e-8));
    assert.ok(gaps.at(-1) > gaps[0] * 2);
    assert.ok(pixels / gaps.length >= 24);
  }
});

test('the scale changes without a jump or a sudden change of slope', () => {
  const axis = chartAxis(data, 1800, 12);
  const step = axis.mainMax * 0.0001;
  const middle = axis.position(axis.mainMax);
  const before = middle - axis.position(axis.mainMax - step);
  const after = axis.position(axis.mainMax + step) - middle;
  assert.ok(before > 0 && after > 0);
  assert.ok(Math.abs(after / before - 1) < 0.0001);
});

test('nonlinear spacing adapts to the sample and is independent of measurement units', () => {
  const axis = chartAxis(data, 1800, 12);
  const millions = chartAxis(data.map(value => value * 1_000_000), 1800, 12);
  for (const value of data) assert.ok(Math.abs(axis.position(value) - millions.position(value * 1_000_000)) < 1e-10);
  const largerTail = chartAxis(data.slice(0, -1).concat(3000), 1800, 12);
  assert.ok(largerTail.max > axis.max);
  assert.notDeepEqual(largerTail.ticks, axis.ticks);
});

test('an extreme outlier does not flatten the moderately large values onto the transition', () => {
  const axis = chartAxis(data.concat(60_000), 1800, 12);
  assert.equal(axis.compressed, true);
  assert.ok(axis.position(200) - axis.position(80) > 0.08);
  assert.ok(axis.position(600) - axis.position(120) > 0.09);
  assert.ok(axis.position(60_000) < 1);
});
