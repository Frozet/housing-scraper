import test from 'node:test';
import assert from 'node:assert/strict';
import {nearestPoint, pointGrid} from '../src/scatter-points.js';

test('tooltip finds the nearest offer across cell edges and at plot extremes', () => {
  const low = {id: 1, x: 12, y: 13};
  const near = {id: 2, x: 17, y: 16};
  const outlier = {id: 3, x: 892, y: 30};
  const grid = pointGrid([low, near, outlier]);
  assert.equal(nearestPoint(grid, 15, 16), near);
  assert.equal(nearestPoint(grid, 12, 12), low);
  assert.equal(nearestPoint(grid, 890, 30), outlier);
  assert.equal(nearestPoint(grid, 400, 80), null);
});
