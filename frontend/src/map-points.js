import {number} from './format.js';

export function priceLabel(minimum, maximum) {
  if (minimum == null) return 'Без цены';
  const divisor = maximum >= 1_000_000 ? 1_000_000 : 1000;
  const suffix = divisor === 1_000_000 ? ' млн ₽' : ' тыс. ₽';
  const low = number(minimum / divisor), high = number(maximum / divisor);
  return (minimum === maximum ? low : `${low}–${high}`) + suffix;
}

function project(latitude, longitude, zoom) {
  const sin = Math.sin(Math.max(-85, Math.min(85, latitude)) * Math.PI / 180);
  const size = 256 * 2 ** zoom;
  return [(longitude + 180) / 360 * size, (.5 - Math.log((1 + sin) / (1 - sin)) / (4 * Math.PI)) * size];
}

export function clusterPoints(points, zoom) {
  const size = zoom < 14 ? 56 : zoom < 18 ? 90 : 0;
  const groups = new Map();
  for (const point of points) {
    const [x, y] = project(point.latitude, point.longitude, zoom);
    const key = size ? `${Math.floor(x / size)}:${Math.floor(y / size)}` : `${point.latitude.toFixed(6)}:${point.longitude.toFixed(6)}`;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(point);
  }
  return [...groups.values()].map(members => {
    const prices = members.filter(point => point.price_min != null);
    return {members, latitude: members.reduce((sum, p) => sum + p.latitude, 0) / members.length,
      longitude: members.reduce((sum, p) => sum + p.longitude, 0) / members.length,
      count: members.reduce((sum, p) => sum + p.count, 0),
      price_min: prices.length ? Math.min(...prices.map(p => p.price_min)) : null,
      price_max: prices.length ? Math.max(...prices.map(p => p.price_max)) : null,
      address_ids: [...new Set(members.flatMap(p => p.address_ids))]};
  });
}
