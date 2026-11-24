import base64
import json

import pytest

from app.price_ranges import MAX_PRICE, PRICE_PREFIX, price_bounds, price_filter, range_url, split_bounds

# These are both real filter strings provided by the user, without context tokens.
@pytest.mark.parametrize('bounds,encoded', [
    ((1_000_000, 2_000_000), 'ASgBAgECAUSSA8YQAUXGmgwdeyJmcm9tIjoxMDAwMDAwLCJ0byI6MjAwMDAwMH0'),
    ((20_000_000, None), 'ASgBAgECAUSSA8YQAUXGmgwYeyJmcm9tIjoyMDAwMDAwMCwidG8iOjB9'),
])
def test_matches_user_supplied_price_filters(bounds, encoded):
    assert price_filter(*bounds) == encoded
    assert price_bounds('https://www.avito.ru/irkutsk/kvartiry/prodam?f=' + encoded) == bounds


@pytest.mark.parametrize('bounds', [(1, 9), (999_999, 1_000_000), (1, None), (MAX_PRICE, None)])
def test_payload_length_changes_with_price_digits(bounds):
    encoded = price_filter(*bounds)
    raw = base64.urlsafe_b64decode(encoded + '=' * (-len(encoded) % 4))
    assert raw[len(PRICE_PREFIX)] == len(raw) - len(PRICE_PREFIX) - 1
    assert price_bounds('?f=' + encoded) == bounds


@pytest.mark.parametrize('suffix', ['?f=invalid', '?f=', '?f=a&f=b', '?f=💰'])
def test_unrecognized_filter_cannot_be_mistaken_for_unfiltered_catalog(suffix):
    with pytest.raises(ValueError, match='фильтр'):
        price_bounds(suffix)


@pytest.mark.parametrize('bounds', [(-1, 10), (10, 9), (1, 0), (MAX_PRICE + 1, None), (True, 10)])
def test_rejects_invalid_bounds(bounds):
    with pytest.raises(ValueError):
        price_filter(*bounds)


def test_ranges_cover_boundaries_and_leave_no_expensive_tail_out():
    assert split_bounds(1, None) == ((1, 19_999_999), (20_000_000, None))
    assert split_bounds(20_000_000, None) == ((20_000_000, 39_999_999), (40_000_000, None))
    assert split_bounds(1, 10) == ((1, 5), (6, 10))
    assert split_bounds(10, 10) is None
    assert split_bounds(MAX_PRICE, None) is None
    left, right = split_bounds(MAX_PRICE - 1, None)
    assert left == (MAX_PRICE - 1, MAX_PRICE - 1) and right == (MAX_PRICE, None)


def test_builds_filter_without_stale_context_or_page_and_keeps_city_priority():
    url = range_url('https://www.avito.ru/irkutsk/kvartiry/prodam?p=30&context=stale&localPriority=1&f=old',
                    1_000_000, 2_000_000)
    assert price_bounds(url) == (1_000_000, 2_000_000)
    assert 'context=' not in url and 'p=' not in url and 'localPriority=0' in url
    assert url.startswith('https://www.avito.ru/irkutsk/kvartiry/prodam-ASgBAgICAUSSA8YQ?')


def test_unknown_binary_schema_and_wrong_length_are_rejected():
    payload = json.dumps({'from': 1, 'to': 2}).encode()
    for raw in (b'unknown' + bytes([len(payload)]) + payload, PRICE_PREFIX + b'\x01' + payload):
        encoded = base64.urlsafe_b64encode(raw).decode()
        with pytest.raises(ValueError):
            price_bounds('?f=' + encoded)
