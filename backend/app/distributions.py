"""Decimal percentiles and histograms of offers, with explicit open tail bins."""
from bisect import bisect_left, bisect_right
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP

CENT = Decimal('.01')


def rounded(value):
    return value.quantize(CENT, rounding=ROUND_HALF_UP) if value is not None else None


def percentile(ordered, probability):
    """Linear interpolation at (n - 1) * p, matching a continuous percentile."""
    if not ordered:
        return None
    position = Decimal(len(ordered) - 1) * Decimal(str(probability))
    left = int(position)
    fraction = position - left
    return ordered[left] + (ordered[min(left + 1, len(ordered) - 1)] - ordered[left]) * fraction


def nice_step(value):
    magnitude = Decimal(10) ** value.adjusted()
    return next(step * magnitude for step in map(Decimal, ['1', '2', '2.5', '5', '10']) if step * magnitude >= value)


def sorted_values(values):
    return sorted(Decimal(value) for value in values if value is not None)


def distribution(values):
    return distribution_ordered(sorted_values(values))


def distribution_ordered(ordered):
    quantiles = {f'p{p}': rounded(percentile(ordered, Decimal(p) / 100)) for p in [10, 25, 75, 90]}
    if not ordered:
        return {**quantiles, 'bins': [], 'popular_ranges': [], 'count': 0}
    if ordered[0] == ordered[-1]:
        bins = [dict(lower=ordered[0], upper=ordered[0], upper_inclusive=True, kind='exact', count=len(ordered))]
    else:
        # Choose a useful central bin width; isolated bad/extreme prices never
        # flatten all ordinary offers into a single bin. Tail counts remain visible.
        low, high = percentile(ordered, '.01'), percentile(ordered, '.99')
        span = high - low
        if span == 0:
            span = max(abs(high) * Decimal('.1'), Decimal(1))
            low, high = max(Decimal(0), low - span / 2), high + span / 2
        iqr = percentile(ordered, '.75') - percentile(ordered, '.25')
        rough = 2 * iqr / Decimal(str(len(ordered) ** (1 / 3))) if iqr else span / 10
        step = nice_step(max(span / 30, min(span / 5, rough), CENT))
        lower = (low / step).to_integral_value(rounding=ROUND_FLOOR) * step
        upper = (high / step).to_integral_value(rounding=ROUND_CEILING) * step
        size = max(1, int((upper - lower) / step))
        upper = lower + size * step
        below, above = ordered[0] < lower, ordered[-1] > upper
        if above and upper == high:
            size += 1
            upper += step
        edges = [lower + index * step for index in range(size + 1)]
        bins = ([dict(lower=None, upper=lower, upper_inclusive=False, kind='below', count=bisect_left(ordered, lower))] if below else [])
        for index in range(size):
            left, right = edges[index:index + 2]
            inclusive = index == size - 1 and not above
            count = (bisect_right(ordered, right) if inclusive else bisect_left(ordered, right)) - bisect_left(ordered, left)
            bins.append(dict(lower=left, upper=right, upper_inclusive=inclusive, kind='range', count=count))
        if above:
            bins.append(dict(lower=upper, upper=None, upper_inclusive=False, kind='above', count=len(ordered) - bisect_left(ordered, upper)))
    largest = max(item['count'] for item in bins)
    return {**quantiles, 'bins': bins, 'popular_ranges': [item for item in bins if item['count'] == largest], 'count': len(ordered)}


def budget_share(prices, minimum=None, maximum=None):
    prices = [price for price in prices if price is not None]
    configured = minimum is not None or maximum is not None
    count = sum((minimum is None or price >= minimum) and (maximum is None or price <= maximum) for price in prices) if configured else None
    return {'minimum': minimum, 'maximum': maximum, 'configured': configured, 'count': count,
            'priced_count': len(prices), 'share': rounded(Decimal(count) * 100 / len(prices)) if configured and prices else None}
