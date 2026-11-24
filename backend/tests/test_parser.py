import json
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import httpx
import pytest

from app.collector import Blocked, check_response
from app.parser import ListingAuction, ListingClosed, ListingOnReview, OutsideCity, ParseError, canonical_url, number, parse_catalog, parse_listing, parse_source_date


def test_all_requested_fields(html, url):
    result = parse_listing(html, url)
    assert result.avito_id == "1234567890"
    assert result.market_type_code == "secondary"
    assert not result.is_open_plan
    assert result.address == "Иркутск, ул. Лермонтова, 100"
    assert result.complex_name == "Академический"
    assert (result.rooms, result.total_area, result.kitchen_area, result.living_area) == (2, Decimal("54.6"), Decimal("10.2"), Decimal("30.5"))
    assert (result.floor, result.floors_total, result.ceiling_height) == (5, 10, Decimal("2.7"))
    assert result.features == {
        "balcony": ["лоджия"], "extras": ["гардеробная", "тёплый пол"], "room_type": ["изолированные"],
        "bathroom": ["раздельный"], "windows": ["во двор", "на улицу"], "finish": ["чистовая"],
        "sale_method": ["свободная"], "sale_conditions": ["ипотека", "материнский капитал"],
    }
    assert result.price == 7800000
    assert result.source_updated_at.isoformat() == "2026-10-15T09:30:00+08:00"


def test_saved_listing_with_hashed_classes(hashed_listing):
    # The hashed address layout has no recognizable address classes or microdata.
    html, url = hashed_listing
    result = parse_listing(html, url)
    assert result.avito_id == "9990000101"
    assert result.address == "ул. Тестовая, д. 14"
    assert result.complex_name == "ЖК «Тестовый квартал»"
    assert result.market_type_code == "new"
    assert (result.rooms, result.total_area, result.kitchen_area, result.living_area) == (3, Decimal("88.6"), 11, 50)
    assert (result.floor, result.floors_total, result.ceiling_height) == (18, 19, Decimal("2.7"))
    assert result.price == 14750000
    assert result.features == {
        "balcony": ["балкон", "лоджия"], "extras": ["гардеробная", "панорамные окна"],
        "room_type": ["изолированные", "смежные"], "bathroom": ["совмещенный", "раздельный"],
        "windows": ["во двор"], "finish": ["без отделки"],
        "sale_method": ["договор купли-продажи"], "sale_conditions": ["возможна ипотека"],
        "house_type": ["монолитно-кирпичный"], "passenger_elevator": ["1"],
        "parking": ["за шлагбаумом во дворе", "гостевая"],
        'construction_status': ['ready'], 'district': ['Правобережный'],
    }
    assert result.construction_year is None  # «Срок сдачи» is a different characteristic.
    assert result.source_updated_at is None  # The page shows publication, not an update.


def test_listing_with_nested_address_is_a_regular_apartment(november_listing):
    result = parse_listing(*november_listing)
    assert result.avito_id == "9990000102"
    assert result.address == "ул. Примерная, з/у 29/2"
    assert result.market_type_code == "new" and result.complex_name == "ЖК «Тестовый берег»"
    assert result.price == 5_250_000 and result.total_area == Decimal("38.7")
    assert (result.rooms, result.floor, result.floors_total) == (1, 4, 11)


def test_studio_and_missing_update_are_not_invented(html, url):
    html = html.replace("Количество комнат: 2", "Количество комнат: студия").replace("Вторичка", "Новостройка")
    html = html.replace('itemprop="dateModified"', 'itemprop="datePublished"')
    result = parse_listing(html, url)
    assert result.rooms == 0
    assert not result.is_open_plan
    assert result.market_type_code == "new"
    assert result.source_updated_at is None


def test_json_ld_and_title_fallback(url):
    node = {"@type": "Product", "name": "1-к. квартира, 32 м², 2/9 эт.",
            "address": {"addressLocality": "Иркутск", "streetAddress": "ул. Ленина, 1"},
            "additionalProperty": [{"name": "Тип жилья", "value": "Новостройка"}],
            "offers": {"price": "5000000", "priceCurrency": "RUB"}}
    data = parse_listing('<script type="application/ld+json">' + json.dumps(node) + '</script>', url)
    assert data.price == 5000000 and data.total_area == 32
    assert data.rooms == 1 and data.floor == 2 and data.floors_total == 9


@pytest.mark.parametrize("value", ["Свободная планировка", "свободная", "свободной планировки"])
def test_open_plan_in_room_count_does_not_become_studio(html, url, value):
    result = parse_listing(html.replace("Количество комнат: 2", "Количество комнат: " + value), url)
    assert result.rooms is None and result.is_open_plan
    assert result.price == 7800000 and result.total_area == Decimal("54.6")


@pytest.mark.parametrize("heading", ["Свободная планировка", "Квартира со свободной планировкой", "Квартира свободной планировки"])
def test_open_plan_title_without_room_field(html, url, heading):
    html = html.replace("<li>Количество комнат: 2</li>", "")
    html = html.replace("2-к. квартира", heading)
    result = parse_listing(html, url)
    assert result.rooms is None and result.is_open_plan


@pytest.mark.parametrize("label", ["Планировка", "Тип планировки"])
def test_open_plan_layout_field_without_room_count(html, url, label):
    html = html.replace("Количество комнат: 2", label + ": свободная").replace("2-к. квартира", "Квартира")
    result = parse_listing(html, url)
    assert result.rooms is None and result.is_open_plan


def test_missing_rooms_without_open_plan_is_still_a_parse_error(html, url):
    html = html.replace("Количество комнат: 2", "Количество комнат: не указано").replace("2-к. квартира", "Квартира")
    # The ordinary fixture includes «Способ продажи: свободная»; that is not a layout.
    with pytest.raises(ParseError, match="количество комнат"):
        parse_listing(html, url)


def test_open_plan_json_ld(url):
    node = {"@type": "Apartment", "name": "Свободная планировка, 48 м², 3/9 эт.",
            "address": {"addressLocality": "Иркутск", "streetAddress": "ул. Ленина, 1"},
            "additionalProperty": [{"name": "Тип жилья", "value": "Новостройка"}],
            "offers": {"price": "5000000", "priceCurrency": "RUB"}}
    result = parse_listing('<script type="application/ld+json">' + json.dumps(node) + '</script>', url)
    assert result.rooms is None and result.is_open_plan


def test_wrong_city_is_rejected(url):
    node = {"@type": "Product", "address": {"addressLocality": "Ангарск"}}
    with pytest.raises(OutsideCity):
        parse_listing('<script type="application/ld+json">' + json.dumps(node) + '</script>', url)


@pytest.mark.parametrize("markup", [
    '<span class="geo-address-new123">Иркутск, ул. Лермонтова, 100</span>',
    '<div class="style-address-new123">Иркутск, ул. Лермонтова, 100</div>',
    '<div data-marker="item-view/address">Иркутск, ул. Лермонтова, 100</div>',
    '<div data-marker="item-view/location">Расположение: Иркутск, ул. Лермонтова, 100 <button>Показать на карте</button></div>',
    '<meta itemprop="address" content="Иркутск, ул. Лермонтова, 100">',
    '<div itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">'
    '<meta itemprop="addressLocality" content="Иркутск">'
    '<meta itemprop="streetAddress" content="ул. Лермонтова, 100"><button>На карте</button></div>',
    '<div data-marker="item-view/item-params"><li>Адрес: Иркутск, ул. Лермонтова, 100</li></div>',
])
def test_address_markup_variants(html, url, markup):
    html = html.replace('<div data-marker="item-view/item-address">Иркутск, ул. Лермонтова, 100</div>', markup)
    assert parse_listing(html, url).address == "Иркутск, ул. Лермонтова, 100"


def test_microdata_address_checks_city(html, url):
    html = html.replace('<div data-marker="item-view/item-address">Иркутск, ул. Лермонтова, 100</div>',
                        '<div itemprop="address"><meta itemprop="addressLocality" content="Ангарск">'
                        '<meta itemprop="streetAddress" content="ул. Ленина, 1"></div>')
    with pytest.raises(OutsideCity):
        parse_listing(html, url)


@pytest.mark.parametrize("address", [
    {"addressLocality": "Иркутск"},
    "Иркутская обл., г. Иркутск",
    "Иркутск",
])
def test_city_alone_is_not_saved_as_an_apartment_address(html, url, address):
    html = html.replace('<div data-marker="item-view/item-address">Иркутск, ул. Лермонтова, 100</div>', '')
    node = {"@type": "Product", "address": address}
    html += '<script type="application/ld+json">' + json.dumps(node) + '</script>'
    with pytest.raises(ParseError, match="поля: адрес"):
        parse_listing(html, url)


def test_recommended_listing_address_is_not_used(html, url):
    html = html.replace('<div data-marker="item-view/item-address">Иркутск, ул. Лермонтова, 100</div>', '')
    html += '<div data-marker="item"><span class="geo-address-abc">Иркутск, другой адрес, 1</span></div>'
    with pytest.raises(ParseError, match="поля: адрес"):
        parse_listing(html, url)


def test_nested_json_ld_address_is_used_without_duplicate_city(html, url):
    html = html.replace('<div data-marker="item-view/item-address">Иркутск, ул. Лермонтова, 100</div>', '')
    node = {"@graph": [{"@type": "RealEstateListing", "itemOffered": {"@type": "Apartment",
             "address": {"addressLocality": "город Иркутск", "streetAddress": "город Иркутск, ул. Лермонтова, 100"}}}]}
    html += '<script type="application/ld+json">' + json.dumps(node) + '</script>'
    assert parse_listing(html, url).address == "город Иркутск, ул. Лермонтова, 100"


def test_layout_change_is_reported(url):
    with pytest.raises(ParseError, match="Не удалось прочитать"):
        parse_listing("<h1>Объявление</h1>", url)


def test_supplied_review_status_is_recognized_without_listing_fields(review_listing):
    html, url = review_listing
    with pytest.raises(ListingOnReview, match="на проверке"):
        parse_listing(html, url)


def test_review_message_in_regular_description_does_not_skip_listing(html, url):
    html = html.replace('</main>', '<p>Оно ещё на проверке.</p></main>')
    assert parse_listing(html, url).price == 7800000


@pytest.mark.parametrize('fixture_name', ['closed_listing', 'expired_listing'])
def test_supplied_closed_status_is_recognized_without_listing_fields(request, fixture_name):
    html, url = request.getfixturevalue(fixture_name)
    with pytest.raises(ListingClosed, match="закрыто"):
        parse_listing(html, url)


@pytest.mark.parametrize('message', ['Объявление закрыто.', 'Объявление истекло.'])
def test_closed_message_in_regular_description_does_not_skip_listing(html, url, message):
    html = html.replace('</main>', '<p>' + message + '</p></main>')
    assert parse_listing(html, url).price == 7800000


def test_supplied_rejected_page_is_unavailable_with_its_own_reason(rejected_listing):
    from app.parser import ListingRejected
    html, url = rejected_listing
    with pytest.raises(ListingRejected, match='отклонено'):
        parse_listing(html, url)


def test_rejection_text_in_description_does_not_archive_active_listing(html, url):
    document = html.replace('</main>', '<p>Оно отклонено, но снова появится в поиске, если его исправят.</p></main>')
    assert parse_listing(document, url).price == 7800000


def test_supplied_deleted_page_is_unavailable_with_its_own_reason(deleted_listing):
    from app.parser import ListingDeleted
    html, url = deleted_listing
    with pytest.raises(ListingDeleted, match='удалено пользователем'):
        parse_listing(html, url)


def test_deletion_text_in_description_does_not_archive_active_listing(html, url):
    document = html.replace('</main>', '<p>Пользователь его удалил.</p></main>')
    assert parse_listing(document, url).price == 7800000


def test_unknown_unavailable_status_is_not_silently_skipped(url):
    with pytest.raises(ParseError, match="Не удалось прочитать"):
        parse_listing('<h1>Объявление не посмотреть</h1><p>Подтвердите, что вы не робот</p>', url)


def test_supplied_blocked_page_is_archivable_with_its_own_reason(blocked_listing):
    from app.parser import ListingBlocked
    with pytest.raises(ListingBlocked, match="заблокировано Авито после проверки"):
        parse_listing(*blocked_listing)


@pytest.mark.parametrize("heading", ["Объявление не посмотреть", "Проверка браузера"])
def test_blocking_text_in_description_does_not_archive_active_listing(html, url, heading):
    document = html.replace('</main>', f'<div><h2>{heading}</h2><p>Оно заблокировано после проверки.</p></div></main>')
    assert parse_listing(document, url).price == 7800000


def test_browser_challenge_is_not_classified_as_blocked_listing(url):
    with pytest.raises(ParseError) as error:
        parse_listing('<h1>Проверка браузера</h1><p>Оно заблокировано после проверки.</p>', url)
    assert not isinstance(error.value, ListingClosed)


@pytest.mark.parametrize("keep_title", [True, False])
def test_supplied_auction_is_skipped_before_required_fields(auction_listing, keep_title):
    html, url = auction_listing
    if not keep_title:
        html = html.replace("Аукцион: ", "")
    with pytest.raises(ListingAuction, match="аукционом"):
        parse_listing(html, url)


def test_auction_title_alone_is_enough_to_skip(url):
    with pytest.raises(ListingAuction):
        parse_listing('<h1>Аукцион: квартира, 45 м²</h1>', url)


def test_auction_in_structured_sale_conditions_is_skipped(html, url):
    html = html.replace('<li>Условия продажи: ипотека, материнский капитал</li>', '')
    node = {"@type": "Product", "additionalProperty": [
        {"name": "Условия продажи", "value": "возможна ипотека, аукцион"}]}
    html += '<script type="application/ld+json">' + json.dumps(node) + '</script>'
    with pytest.raises(ListingAuction):
        parse_listing(html, url)


def test_auction_mentions_in_description_and_recommendations_do_not_skip_listing(html, url):
    html = html.replace('</main>', '<p>Это не аукцион. Без торгов.</p>'
                        '<div data-marker="item"><h2>Аукцион: другая квартира</h2></div></main>')
    assert parse_listing(html, url).price == 7800000


def test_negated_auction_in_sale_conditions_does_not_skip_listing(html, url):
    html = html.replace('ипотека, материнский капитал', 'ипотека, не аукцион')
    assert parse_listing(html, url).price == 7800000


def test_catalog_deduplicates_and_ignores_other_cities(url):
    html = ''.join(f'<div data-marker="item"><a data-marker="item-title" href="{u}">Квартира</a></div>'
                   for u in (url, url + "?context=x", url.replace("/irkutsk/", "/angarsk/")))
    html += '<a data-marker="pagination-button/next" href="?p=2">Далее</a>'
    assert parse_catalog(html) == ([url], True)


def test_empty_catalog_must_be_explicit():
    assert parse_catalog('<div data-marker="search-results/empty"></div>') == ([], False)
    with pytest.raises(ParseError):
        parse_catalog('<div>Неизвестная страница</div>')


def test_supplied_catalog_with_only_other_cities_ends_local_results(other_city_catalog):
    source = 'https://www.avito.ru/usole-sibirskoe/kvartiry/prodam?p=2'
    assert parse_catalog(other_city_catalog, 2, source_url=source, unknown_next=None) == ([], False)


def test_last_local_cards_are_saved_and_recommendations_are_excluded(other_city_catalog, url):
    local = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    document = other_city_catalog.replace('id="bx_serp-item-list">', 'id="bx_serp-item-list">' + local)
    # Even a recommended card matching the city URL does not belong to the local results.
    document = document.replace('/sredniy/', '/irkutsk/')
    assert parse_catalog(document, 2, source_url='https://www.avito.ru/irkutsk/kvartiry/prodam?p=2',
                         unknown_next=None) == ([url], False)


@pytest.mark.parametrize('replacement', ['<h2>Другие предложения</h2>',
    '<div data-marker="item"><h2>2 357 объявлений есть в других городах</h2></div>'])
def test_unknown_empty_catalog_is_not_completed_from_recommendation_cards(other_city_catalog, replacement):
    document = other_city_catalog.replace('<h2>2 357 объявлений есть в других городах</h2>', replacement)
    with pytest.raises(ParseError):
        parse_catalog(document, 2, source_url='https://www.avito.ru/usole-sibirskoe/kvartiry/prodam?p=2')


def test_broken_local_links_are_still_reported_with_other_cities_block(other_city_catalog):
    broken = '<div data-marker="item"><a data-marker="item-title" href="/changed-layout">Квартира</a></div>'
    document = other_city_catalog.replace('id="bx_serp-item-list">', 'id="bx_serp-item-list">' + broken)
    with pytest.raises(ParseError, match='ссылки на объявления'):
        parse_catalog(document, 2, source_url='https://www.avito.ru/usole-sibirskoe/kvartiry/prodam?p=2')


def test_numeric_pagination_only_has_next_before_last_page(url):
    html = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    html += '<a data-marker="pagination-button" href="?p=2">2</a>'
    assert parse_catalog(html, 1)[1] is True
    assert parse_catalog(html, 2)[1] is False


def test_browser_pagination_distinguishes_unknown_from_last_page(url):
    html = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    assert parse_catalog(html, unknown_next=None)[1] is None
    assert parse_catalog(html + '<button data-marker="pagination-button/nextPage">Далее</button>', unknown_next=None)[1] is True
    assert parse_catalog(html + '<button data-marker="pagination-button/nextPage" disabled>Далее</button>', unknown_next=None)[1] is False


def test_last_page_is_confirmed_by_active_pagination_without_next_link(url):
    html = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    html += '<nav data-marker="pagination"><a data-marker="pagination-button/page(29)" href="?p=29">29</a>'
    html += '<span data-marker="pagination-button/page(30)" aria-current="page">30</span></nav>'
    assert parse_catalog(html, 30, unknown_next=None)[1] is False
    arrow = '<a href="?p=31" aria-label="Вперёд"><svg></svg></a>'
    assert parse_catalog(html.replace('</nav>', arrow + '</nav>'), 30, unknown_next=None)[1] is True
    unknown = '<button aria-label="Вперёд"><svg></svg></button>'
    assert parse_catalog(html.replace('</nav>', unknown + '</nav>'), 30, unknown_next=None)[1] is None


def test_rendered_page_takes_precedence_over_rewritten_url(url):
    html = f'<div data-marker="item"><a data-marker="item-title" href="{url}">Квартира</a></div>'
    html += '<nav><a href="?p=30">30</a><span aria-current="page">31</span></nav>'
    assert parse_catalog(html, 31, unknown_next=None, source_url="https://www.avito.ru/irkutsk/kvartiry/prodam")[0] == [url]
    with pytest.raises(ParseError, match="Ожидалась страница каталога 30, открыта 31"):
        parse_catalog(html, 30, source_url="https://www.avito.ru/irkutsk/kvartiry/prodam?p=30")


@pytest.mark.parametrize("value", ["https://evil.example/irkutsk/kvartiry/foo_123", "http://www.avito.ru/irkutsk/kvartiry/foo_123", "https://www.avito.ru/irkutsk/doma/foo_123"])
def test_non_listing_urls_are_rejected(value):
    assert canonical_url(value) is None


@pytest.mark.parametrize("value", ["20-30 м²", "от 20 м²", "0", "не указана"])
def test_ambiguous_areas_are_not_guessed(value):
    assert number(value) is None


def test_relative_update_date():
    now = datetime(2026, 10, 15, 22, 0, tzinfo=ZoneInfo("Asia/Irkutsk"))
    assert parse_source_date("Обновлено вчера в 18:30", now).isoformat() == "2026-10-14T18:30:00+08:00"
    assert parse_source_date("Обновлено 31 декабря в 12:00", now).year == 2025
    assert parse_source_date("Размещено сегодня в 10:00", now) is None


@pytest.mark.parametrize("status", [403, 429])
def test_block_is_explicit(status):
    with pytest.raises(Blocked):
        check_response(httpx.Response(status, text="blocked"))


def test_captcha_with_success_status_is_blocked():
    with pytest.raises(Blocked):
        check_response(httpx.Response(200, text="<title>Доступ ограничен</title>"))


@pytest.mark.parametrize('offers', ['по запросу', 123, [None, 'по запросу'], {'price': '9000000'}])
def test_malformed_structured_offer_does_not_hide_a_visible_price(html, url, offers):
    node = {'@type': ['Product', {'wrong': 'shape'}], 'offers': offers}
    html += '<script type="application/ld+json">' + json.dumps(node) + '</script>'
    assert parse_listing(html, url).price == 7800000


@pytest.mark.parametrize('old,new,field', [
    ('54,6', '0.001', 'общая площадь'),
    ('54,6', '99999999.995', 'общая площадь'),
    ('7800000', '9223372036854775808', 'цена'),
])
def test_parser_rejects_numbers_that_do_not_fit_storage(html, url, old, new, field):
    document = html.replace(old, new).replace('54.6', new if old == '54,6' else '54.6')
    with pytest.raises(ParseError, match=field):
        parse_listing(document, url)
