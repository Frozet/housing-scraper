// Two fields keep price independent of listing density: Σ(price × weight) / Σweight.
// A compact smooth kernel leaves areas without nearby observations transparent.
export const HEAT_COLORS = ['#1a9850', '#91cf60', '#fee08b', '#fc8d59', '#d73027'];
const RGB = [[26,152,80], [145,207,96], [254,224,139], [252,141,89], [215,48,39]];
const clamp = (value, low, high) => Math.max(low, Math.min(high, value));
export const hasHeatPrice = point => Number.isFinite(point.price_mean) && point.price_mean > 0
  && Number.isFinite(point.priced_count) && point.priced_count > 0;

function percentile(sorted, fraction) {
  const index = (sorted.length - 1) * fraction, low = Math.floor(index), high = Math.ceil(index);
  return sorted[low] + (sorted[high] - sorted[low]) * (index - low);
}

export function heatDomain(points) {
  const prices = points.filter(hasHeatPrice).map(point => point.price_mean).sort((a,b) => a-b);
  if (!prices.length) return null;
  // The full filtered city sets the scale; panning never changes a house's color.
  // Saturating the tails preserves contrast when a few listings have extreme prices.
  const low = percentile(prices, .05), high = percentile(prices, .95);
  return low === high && prices[0] !== prices.at(-1) ? {low: prices[0], high: prices.at(-1)} : {low, high};
}

export function heatColor(price, domain) {
  const value = domain.high === domain.low ? .5 : clamp((price-domain.low)/(domain.high-domain.low), 0, 1);
  const position = value * (RGB.length-1), low = Math.min(RGB.length-2, Math.floor(position));
  const t = position-low, blend = t*t*(3-2*t);
  return RGB[low].map((channel, index) => Math.round(channel + (RGB[low+1][index]-channel)*blend));
}

export function heatRadius(zoom, latitude) {
  const metersPerPixel = 156543.03392 * Math.cos(latitude*Math.PI/180) / 2**zoom;
  return clamp(350/metersPerPixel, 32, 100);
}

export function heatRaster(points, width, height, radius, domain, step=4) {
  const columns = Math.ceil(width/step), rows = Math.ceil(height/step), size = columns*rows;
  const pixels = new Uint8ClampedArray(size*4);
  if (!domain || !size || radius <= 0) return {pixels, width: columns, height: rows};
  const weightedPrices = new Float64Array(size), weights = new Float64Array(size), coverage = new Float32Array(size);
  const radiusSquared = radius*radius;
  for (const point of points) {
    if (!hasHeatPrice(point) || !Number.isFinite(point.x) || !Number.isFinite(point.y)) continue;
    const x0 = Math.max(0, Math.floor((point.x-radius)/step)), x1 = Math.min(columns-1, Math.ceil((point.x+radius)/step));
    const y0 = Math.max(0, Math.floor((point.y-radius)/step)), y1 = Math.min(rows-1, Math.ceil((point.y+radius)/step));
    for (let y=y0; y<=y1; y++) {
      const dy = (y+.5)*step-point.y;
      for (let x=x0; x<=x1; x++) {
        const dx = (x+.5)*step-point.x, distanceSquared = dx*dx+dy*dy;
        if (distanceSquared >= radiusSquared) continue;
        const kernel = (1-distanceSquared/radiusSquared)**3, index = y*columns+x;
        const weight = kernel*point.priced_count;
        weightedPrices[index] += point.price_mean*weight;
        weights[index] += weight;
        coverage[index] += kernel;
      }
    }
  }
  for (let index=0; index<size; index++) {
    if (weights[index] === 0) continue;
    const rgb = heatColor(weightedPrices[index]/weights[index], domain);
    const offset = index*4;
    pixels[offset] = rgb[0]; pixels[offset+1] = rgb[1]; pixels[offset+2] = rgb[2];
    pixels[offset+3] = Math.round(180 * (1-Math.exp(-3*coverage[index])));
  }
  return {pixels, width: columns, height: rows};
}
