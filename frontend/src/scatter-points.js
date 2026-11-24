const cellSize = 8;
const key = (x, y) => `${x}:${y}`;

export function pointGrid(points) {
  const cells = new Map();
  for (const point of points) {
    const cell = key(Math.floor(point.x / cellSize), Math.floor(point.y / cellSize));
    if (!cells.has(cell)) cells.set(cell, []);
    cells.get(cell).push(point);
  }
  return cells;
}

export function nearestPoint(cells, x, y) {
  const column = Math.floor(x / cellSize), row = Math.floor(y / cellSize);
  let nearest = null, best = cellSize ** 2;
  for (let i = column - 1; i <= column + 1; i++) {
    for (let j = row - 1; j <= row + 1; j++) {
      for (const point of cells.get(key(i, j)) || []) {
        const distance = (point.x - x) ** 2 + (point.y - y) ** 2;
        if (distance <= best) {
          nearest = point;
          best = distance;
        }
      }
    }
  }
  return nearest;
}
