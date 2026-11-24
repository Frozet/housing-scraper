// Reserve enough space for the tail to show differences between expensive/large listings.
const MAIN_SHARE = 0.6;

function niceStep(span, intervals) {
  const rough = span / intervals;
  const magnitude = 10 ** Math.floor(Math.log10(rough));
  return [1, 2, 2.5, 5, 10].reduce((best, value) =>
    Math.abs(Math.log(value * magnitude / rough)) < Math.abs(Math.log(best * magnitude / rough)) ? value : best) * magnitude;
}

function rounded(value) {
  return Number(value.toPrecision(12));
}

export function chartAxis(values, pixels, intervals = 10, mode = 'auto') {
  const ordered = values.filter(value => Number.isFinite(value) && value >= 0).sort((a, b) => a - b);
  const maximum = Math.max(ordered.at(-1) || 0, 0.01);
  const intervalCount = Math.max(2, Math.min(12, intervals));
  const fullStep = niceStep(maximum * 1.04, intervalCount);
  const max = rounded(Math.ceil(maximum * 1.04 / fullStep) * fullStep);
  const percentile = ordered[Math.ceil(ordered.length * 0.95) - 1] || maximum;
  const mainIntervals = Math.max(2, Math.round(intervalCount * MAIN_SHARE));
  const mainStep = niceStep(percentile, mainIntervals);
  const mainMax = rounded(mainIntervals * mainStep);
  const compressed = mode === 'auto' && ordered.length >= 20 && maximum > mainMax * 2.5;
  if (!compressed) {
    const ticks = Array.from({length: Math.round(max / fullStep) + 1}, (_, index) => rounded(index * fullStep));
    return {max, mainMax: max, compressed, ticks, position: value => value / max};
  }

  const count = Math.max(4, Math.min(intervalCount, Math.floor(pixels / 24)));
  const linearCount = Math.max(1, Math.round(count * MAIN_SHARE));
  const linearStep = mainMax / linearCount;
  const tailCount = count - linearCount;
  const tailSpan = linearStep * tailCount;
  const ratio = (max - mainMax) / tailSpan;
  // Fit sinh(k) / k to the observed tail. Its slope matches the linear part
  // and its curvature starts at zero; even orders-of-magnitude outliers fit smoothly.
  let low = 0, high = 1;
  while (Math.sinh(high) / high < ratio) high *= 2;
  for (let iteration = 0; iteration < 50; iteration++) {
    const middle = (low + high) / 2;
    if (Math.sinh(middle) / middle < ratio) low = middle;
    else high = middle;
  }
  const curve = (low + high) / 2;
  const valueAt = division => division <= linearCount ? linearStep * division
    : mainMax + tailSpan * Math.sinh(curve * (division - linearCount) / tailCount) / curve;
  const position = value => {
    if (value <= mainMax) return value / linearStep / count;
    if (value >= max) return 1;
    return (linearCount + tailCount * Math.asinh((value - mainMax) * curve / tailSpan) / curve) / count;
  };
  const ticks = Array.from({length: count + 1}, (_, index) => rounded(valueAt(index)));
  return {max, mainMax, compressed, ticks, position};
}
