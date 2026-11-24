"""Разбор HTML отделён от сети: его можно проверять на сохранённых страницах."""
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from app.geography import coordinates

from app.catalog_navigation import CURRENT_PAGE_SELECTOR, catalog_page, rendered_page
from app.property_types import CATEGORIES, HOUSE_FEATURES
from app.geography import distance_km
from app.markup import (
    ADDRESS_SELECTORS, LISTING_PARAM_ROWS_SELECTOR, LISTING_STATUS_HEADING_SELECTOR,
    LISTING_STATUS_MESSAGE_SELECTOR, listing_is_auction, listing_status,
)

MOSCOW = ZoneInfo("Europe/Moscow")
IRKUTSK = ZoneInfo("Asia/Irkutsk")
CATALOG_URL = "https://www.avito.ru/irkutsk/kvartiry/prodam"
BIGINT_MAX = 2**63 - 1
INTEGER_MAX = 2**31 - 1
FEATURE_LABELS = {
    "балкон или лоджия": "balcony", "дополнительно": "extras", "тип комнат": "room_type",
    "санузел": "bathroom", "окна": "windows", "отделка": "finish", "ремонт": "finish",
    "способ продажи": "sale_method", "условия продажи": "sale_conditions",
    "тип дома": "house_type", "парковка": "parking",
    "пассажирский лифт": "passenger_elevator", "пассажирские лифты": "passenger_elevator",
}


class ParseError(ValueError):
    pass


class ListingSkipped(ParseError):
    """An explicitly excluded listing; this is not a parser failure."""


class OutsideCity(ListingSkipped):
    pass


class ListingOnReview(ListingSkipped):
    pass


class ListingClosed(ListingSkipped):
    pass


class ListingRejected(ListingClosed):
    pass


class ListingDeleted(ListingClosed):
    pass


class ListingBlocked(ListingClosed):
    pass


class ListingAuction(ListingSkipped):
    pass


@dataclass
class ListingData:
    avito_id: str
    url: str
    title: str
    market_type_code: str
    address: str
    rooms: int | None
    total_area: Decimal
    price: int
    property_kind: str = "apartment"
    land_area: Decimal | None = None
    distance_to_center_km: Decimal | None = None
    source_distance_km: Decimal | None = None
    latitude: Decimal | None = None
    longitude: Decimal | None = None
    is_open_plan: bool = False
    complex_name: str | None = None
    kitchen_area: Decimal | None = None
    living_area: Decimal | None = None
    floor: int | None = None
    floors_total: int | None = None
    ceiling_height: Decimal | None = None
    construction_year: int | None = None
    source_updated_at: datetime | None = None
    features: dict[str, list[str]] = field(default_factory=dict)


def parse_catalog_count(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.select('[data-marker="page-title/count"], [data-marker="catalog-serp/count"], [data-marker="search-title/count"]'):
        value = clean(tag.get_text(" ", strip=True))
        match = re.fullmatch(r"(?:всего\s*)?(\d[\d\s]*)(?:\s*(?:объявлени[еяй]|предложени[еяй]))?", value, re.I)
        if match:
            return int(re.sub(r"\s", "", match[1]))
    # Some streamed catalog pages keep the same counter only in serialized state.
    counts = set(re.findall(r'"mainCount"\s*:\s*(\d+)', html))
    if len(counts) == 1:
        return int(counts.pop())
    if soup.select_one('[data-marker="search-results/empty"], [data-marker="search-results/empty-title"]'):
        return 0
    raise ParseError("Не удалось прочитать число объявлений для ценовых групп. Выборка не заменяется обычным сбором.")


def clean(value) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\xa0", " ")).strip()


def canonical_url(value: str, base: str = CATALOG_URL) -> str | None:
    try:
        parts = urlsplit(urljoin(base, value))
        if (parts.scheme != "https" or parts.hostname not in {"avito.ru", "www.avito.ru"}
                or parts.username or parts.password or parts.port not in {None, 443}):
            return None
    except ValueError:
        return None
    base_parts = urlsplit(base).path.strip("/").split("/")
    category = base_parts[1] if len(base_parts) > 1 else 'kvartiry'
    if category not in CATEGORIES.values():
        return None
    locality = r'[a-z0-9_-]+' if category == CATEGORIES['house'] else re.escape(base_parts[0])
    match = re.fullmatch(rf"/{locality}/{category}/[^/]+_([0-9]{{1,19}})/?", parts.path)
    if not match or not 0 < int(match[1]) <= BIGINT_MAX:
        return None
    return urlunsplit(("https", "www.avito.ru", parts.path.rstrip("/"), "", ""))


def html_document(html):
    return html if isinstance(html, BeautifulSoup) else BeautifulSoup(html, "html.parser")


def client_redirect(html: str | BeautifulSoup) -> str | None:
    """Avito's streamed router can send a redirect as JSON inside an HTTP 200 page."""
    soup = html_document(html)
    for script in soup.select('script[type="mime/invalid"], script[type="application/json"]'):
        try:
            payload = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        loader = payload.get("loaderData")
        data = loader.get("data") if isinstance(loader, dict) else None
        if not isinstance(data, dict):
            continue
        status = data.get("status")
        code = status.get("code") if isinstance(status, dict) else None
        if data.get("redirected") is True and code in (301, 302, 303, 307, 308):
            target = data.get("url")
            if not isinstance(target, str) or not target.strip():
                raise ParseError("Авито вернул перенаправление без адреса назначения.")
            return target
    return None


def parse_catalog(html: str | BeautifulSoup, page_number: int = 1, *, unknown_next: bool | None = False,
                  source_url: str | None = None) -> tuple[list[str], bool | None]:
    soup = html_document(html)
    current_tags = soup.select(CURRENT_PAGE_SELECTOR)
    current_texts = [tag.get_text(" ", strip=True) for tag in current_tags]
    if source_url is not None and catalog_page(source_url, current_texts) != page_number:
        actual = catalog_page(source_url, current_texts)
        raise ParseError(f"Ожидалась страница каталога {page_number}, открыта {actual or 'неизвестная'}. Прогресс сохранён.")
    catalog = soup.select_one('[data-marker="catalog-serp"]')
    cards = (catalog if catalog is not None else soup).select('[data-marker="item"]')
    recommendations = catalog.find_next_sibling() if catalog is not None else None
    # Avito paginates nearby-city recommendations after the local results end.
    # Their "next page" control must not keep the current price band open.
    local_results_ended = recommendations is not None and any(
        re.fullmatch(r"(?:\d[\d ]* )?объявлени[еяй] есть в других городах", clean(heading.get_text(" ", strip=True)), re.I)
        and heading.find_parent(attrs={"data-marker": "item"}) is None
        for heading in recommendations.select("h2")
    )
    empty = local_results_ended or soup.select_one(
        '[data-marker="search-results/empty"], [data-marker="search-results/empty-title"]') is not None
    urls = []
    for card in cards:
        link = card.select_one('a[data-marker="item-title"], a[itemprop="url"]')
        if link and (url := canonical_url(link.get("href", ""), source_url or CATALOG_URL)) and url not in urls:
            urls.append(url)
    if not cards and not empty:
        raise ParseError("Не найдены карточки каталога. Возможно, изменилась разметка Авито или требуется проверка доступа.")
    if cards and not urls:
        raise ParseError("Карточки каталога найдены, но ссылки на объявления выбранного рынка и города не распознаны. Проверьте разметку ссылок Авито.")
    if empty:
        return urls, False
    next_link = soup.select_one('[data-marker="pagination-button/next"], [data-marker="pagination-button/nextPage"], a[rel="next"], a[aria-label="Следующая страница"]')
    if not next_link:
        next_link = next((a for a in soup.select('a[href*="p="]')
                          if clean(a.get_text()).lower() in {"следующая", "далее", "следующая страница"}), None)
    if next_link:
        return urls, next_link.get("aria-disabled") != "true" and not next_link.has_attr("disabled")
    if not cards:
        return urls, False
    pages = set()
    for link in soup.select('[data-marker^="pagination-button"]'):
        if link.get("aria-disabled") != "true" and not link.has_attr("disabled"):
            next_pages = parse_qs(urlsplit(link.get("href", "")).query).get("p", [])
            pages.update(int(value) for value in next_pages if value.isdigit())
            text = clean(link.get_text())
            if text.isdigit():
                pages.add(int(text))
    if any(page > page_number for page in pages):
        return urls, True
    current = rendered_page(current_texts)
    if current == page_number:
        active = next((tag for tag in current_tags if clean(tag.get_text()).isdigit()
                       and int(clean(tag.get_text())) == current), None)
        scope = active.find_parent(lambda tag: tag.name == "nav" or tag.get("role") == "navigation"
                                   or tag.get("data-marker") == "pagination") if active else None
        if scope is None and active:
            for parent in active.parents:
                if parent.name in {"body", "html", "[document]"}:
                    break
                if len(parent.select('[data-marker^="pagination-button"]')) >= 2:
                    scope = parent
                    break
        if scope:
            unknown_control = False
            visible_pages = {current}
            for control in scope.select('a, button, [role="button"]'):
                if control.has_attr("disabled") or control.get("aria-disabled") == "true":
                    continue
                href = control.get("href", "")
                query = parse_qs(urlsplit(href).query)
                linked_page = rendered_page(query.get("p", []))
                text_page = rendered_page([control.get_text(" ", strip=True)])
                if linked_page or text_page:
                    visible_pages.add(linked_page or text_page)
                elif not ("prev" in control.get("data-marker", "").lower()
                          or "предыдущ" in control.get("aria-label", "").lower()):
                    unknown_control = True
            if any(page > current for page in visible_pages):
                return urls, True
            if not unknown_control and any(page < current for page in visible_pages):
                return urls, False
    # A truncated number window alone cannot prove that this is the last page.
    return urls, unknown_next


def number(value: str | None) -> Decimal | None:
    value = clean(value).replace(",", ".")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:м²|м2|м|кв\.?\s*м\.?)?", value)
    if not match:
        return None
    result = Decimal(match[1])
    return result if result > 0 else None



def house_number(value, unit):
    value = clean(value).casefold().replace(',', '.')
    pattern = r'(\d+(?:\.\d+)?)\s*(сот\.?|сотк[аи]?|га|м²|м2)' if unit == 'land' else r'(\d+(?:\.\d+)?)\s*(км|м)'
    match = re.fullmatch(pattern, value)
    if not match:
        return None
    result = Decimal(match[1])
    if result >= Decimal('1e13'):
        raise ParseError("Число вне допустимого диапазона: площадь участка или расстояние.")
    if unit == 'land':
        result *= Decimal('100') if match[2] == 'га' else Decimal('.01') if match[2] in {'м²', 'м2'} else 1
    elif match[2] == 'м':
        result /= 1000
    if result == 0 and unit == 'land':
        return None
    return stored_decimal(result, 12 if unit == 'land' else 9,
                          'площадь участка' if unit == 'land' else 'расстояние до города',
                          allow_zero=unit == 'distance')


def stored_decimal(value, precision, label, *, allow_zero=False):
    """Validate the value PostgreSQL will store, including rounding to two places."""
    if value is None:
        return None
    limit = Decimal(10) ** (precision - 2)
    if value >= limit or value < 0:
        raise ParseError(f"Число вне допустимого диапазона: {label}.")
    rounded = value.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
    if rounded >= limit or not (allow_zero or rounded > 0):
        raise ParseError(f"Число вне допустимого диапазона: {label}.")
    return rounded


def stored_integer(value, label):
    if not re.fullmatch(r'-?[0-9]{1,10}', value):
        raise ParseError(f"Число вне допустимого диапазона: {label}.")
    result = int(value)
    if not -INTEGER_MAX - 1 <= result <= INTEGER_MAX:
        raise ParseError(f"Число вне допустимого диапазона: {label}.")
    return result

def parse_source_date(value: str | None, now: datetime | None = None) -> datetime | None:
    value = clean(value)
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # ISO dates without an offset on Russian pages are interpreted in Moscow time.
        return result if result.tzinfo else result.replace(tzinfo=MOSCOW)
    except ValueError:
        pass
    now = (now or datetime.now(IRKUTSK)).astimezone(IRKUTSK)
    lower = value.lower().replace("ё", "е")
    lower = re.sub(r"^обновлено\s*:?\s*", "", lower)
    match = re.fullmatch(r"(сегодня|вчера)\s*,?\s*(?:в\s*)?(\d{1,2}):(\d{2})", lower)
    if match:
        date = now - timedelta(days=match[1] == "вчера")
        try:
            return date.replace(hour=int(match[2]), minute=int(match[3]), second=0, microsecond=0)
        except ValueError:
            return None
    months = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    match = re.fullmatch(r"(\d{1,2}) (" + "|".join(months) + r")(?: (\d{4}))?(?:\s*(?:г\.)?\s*,?\s*(?:в\s*)?(\d{1,2}):(\d{2}))?", lower)
    if match:
        try:
            result = datetime(int(match[3] or now.year), months.index(match[2]) + 1, int(match[1]),
                              int(match[4] or 0), int(match[5] or 0), tzinfo=IRKUTSK)
            if not match[3] and result > now + timedelta(days=1):
                result = result.replace(year=result.year - 1)
            return result
        except ValueError:
            return None
    return None


def _structured_nodes(soup):
    nodes = []
    def visit(obj):
        if isinstance(obj, list):
            for value in obj:
                visit(value)
        elif isinstance(obj, dict):
            nodes.append(obj)
            for key in ("@graph", "mainEntity", "itemOffered"):
                if key in obj:
                    visit(obj[key])
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            visit(json.loads(script.string or script.get_text()))
        except (ValueError, TypeError):
            continue
    return nodes


def _check_city(city, expected="Иркутск"):
    if expected is None:
        return
    if city and re.sub(r"^(?:город\s+|г(?:\.\s*|\s+))", "", city.casefold()).replace("ё", "е") != expected.casefold().replace("ё", "е"):
        raise OutsideCity(f"Объявление находится за пределами выбранного города ({expected}).")


def _address_text(value, city_name="Иркутск"):
    value = clean(value)
    value = re.sub(r"^(?:адрес|расположение|местоположение)(?:\s*:\s*|\s+)", "", value, flags=re.I)
    value = re.split(r"\s*(?:показать (?:на )?карт[еу]|на карте|скрыть карту)\b", value, maxsplit=1, flags=re.I)[0]
    value = value.strip(" ,;·")
    # Visible addresses can name a locality even when JSON-LD omits it.
    for part in re.split(r"\s*[,;]\s*", value):
        locality = re.sub(r"^(?:город\s+|г(?:\.\s*|\s+))", "", part.casefold()).replace("ё", "е")
        if city_name is not None and locality in city_names():
            _check_city(part, city_name)
    # A locality alone is not the apartment's address.
    parts = [re.sub(r"^(?:город\s+|г(?:\.\s*|\s+))", "", part.casefold()).replace(".", "")
             for part in re.split(r"\s*[,;]\s*", value)]
    localities = {"", "россия", "рф", "российская федерация", (city_name or "").casefold(), "иркутская область", "иркутская обл"}
    return value if len(value) <= 600 and any(part not in localities for part in parts) else ""


@lru_cache
def city_names():
    return {row["name"].casefold().replace("ё", "е") for row in
            json.loads((Path(__file__).parent / "data/cities.json").read_text())}


def _schema_address(value, city_name="Иркутск"):
    if isinstance(value, str):
        return _address_text(value, city_name)
    if not isinstance(value, dict):
        return ""
    city = clean(value.get("addressLocality"))
    _check_city(city, city_name)
    street = _address_text(value.get("streetAddress"), city_name)
    if not street:
        return ""
    return street if not city or city.casefold() in street.casefold() else f"{city}, {street}"


def _listing_address(main, soup, product, details, params, city_name="Иркутск"):
    structured = details.get("address") or product.get("address")
    # Validate an explicit locality even when the visible address takes precedence.
    fallback = _schema_address(structured, city_name)
    for scope in (main, soup) if main is not soup else (soup,):
        for selector in ADDRESS_SELECTORS:
            for tag in scope.select(selector):
                if tag.find_parent(attrs={"data-marker": "item"}):
                    continue  # Addresses of recommended listings belong to other apartments.
                if "address" in tag.get("itemprop", "").split():
                    def prop(name):
                        child = tag.select_one(f'[itemprop~="{name}"]')
                        return clean(child.get("content") or child.get_text(" ", strip=True)) if child else ""
                    address = _schema_address({"addressLocality": prop("addressLocality"),
                                               "streetAddress": prop("streetAddress")}, city_name)
                    if address:
                        return address
                value = tag.get("content") or tag.get_text(" ", strip=True)
                if address := _address_text(value, city_name):
                    return address
    return fallback or _address_text(params.get("адрес"), city_name)


def _listing_coordinates(soup, product, details, avito_id):
    for tag in soup.select('[data-map-lat][data-map-lon]'):
        if tag.find_parent(attrs={"data-marker": "item"}) or tag.get('data-item-id', avito_id) != avito_id:
            continue
        if point := coordinates(tag.get('data-map-lat'), tag.get('data-map-lon')):
            return point
    for obj in (details, product):
        geo = obj.get('geo')
        if isinstance(geo, dict) and (point := coordinates(geo.get('latitude'), geo.get('longitude'))):
            return point
    return None, None


def _listing_parameters(main, soup):
    # Visible parameters take precedence over structured metadata.
    params = {}
    for row in main.select(LISTING_PARAM_ROWS_SELECTOR):
        if row.find_parent(attrs={"data-marker": "item"}):
            continue
        label, sep, value = clean(row.get_text(" ", strip=True)).partition(":")
        if sep:
            params[clean(label).lower()] = clean(value)
    for term in main.select("dt"):
        if term.find_parent(attrs={"data-marker": "item"}):
            continue
        value = term.find_next_sibling("dd")
        if value:
            params[clean(term.get_text()).rstrip(":").lower()] = clean(value.get_text(" "))
    nodes = _structured_nodes(soup)
    supported = {"Product", "Apartment", "Residence", "RealEstateListing", "House", "SingleFamilyResidence"}
    def is_product(node):
        kind = node.get("@type")
        return any(isinstance(value, str) and value in supported
                   for value in (kind if isinstance(kind, list) else [kind]))
    product = next((node for node in nodes if is_product(node)), {})
    details = product.get("itemOffered", product)
    if not isinstance(details, dict):
        details = product
    for obj in (product, details):
        props = obj.get("additionalProperty", [])
        for prop in props if isinstance(props, list) else [props]:
            if isinstance(prop, dict) and prop.get("name") and prop.get("value") is not None:
                params.setdefault(clean(prop["name"]).lower(), clean(str(prop["value"])))
    return params, product, details


def _listing_features(main, params, property_kind, title, address):
    features = {}
    labels = {**FEATURE_LABELS, **({name.casefold(): code for code, name in HOUSE_FEATURES.items()} if property_kind == 'house' else {})}
    for label, code in labels.items():
        if value := params.get(label):
            values = list(dict.fromkeys(clean(part) for part in re.split(r"[,;]", value) if clean(part)))
            if code in {"house_type", "parking", "passenger_elevator"}:
                values = [part.casefold().replace("ё", "е") for part in values]
                values = [part for part in values if part not in {"не указано", "неизвестно", "нет данных", "—", "-"}]
            if code == "passenger_elevator":
                values = ["нет" if part in {"0", "отсутствует", "отсутствуют", "без лифта"} else
                          match[1] if (match := re.fullmatch(r"(\d+)\s*(?:шт\.?|лифт(?:а|ов)?)?", part)) else part
                          for part in values]
                values = ["нет" if part == "0" else part for part in values]
            if values:
                features[code] = list(dict.fromkeys(features.get(code, []) + values))
    if property_kind == 'house' and 'object_type' not in features:
        kind_match = re.match(r'^(Дом|Дача|Коттедж|Таунхаус)\b', title, re.I)
        if kind_match:
            features['object_type'] = [kind_match[1].capitalize()]
    from app.offer_classification import explicit_offer_features
    address_block = main.select_one('#item-view-address') or main.select_one('[data-marker="item-view/item-address"]')
    if address_block:
        for text in address_block.stripped_strings:
            if re.fullmatch(r'(?:р-н|район)\s+[^,;]{1,80}', clean(text), re.I):
                params.setdefault('район', clean(text))
                break
    features.update(explicit_offer_features(params, address_block.get_text(' ', strip=True) if address_block else address))
    return features


def parse_listing(html: str | BeautifulSoup, url: str, now: datetime | None = None, *, city_name="Иркутск", city_slug="irkutsk",
                  property_kind="apartment", city_center=None, radius_km=None) -> ListingData:
    category = CATEGORIES[property_kind]
    url = canonical_url(url, f"https://www.avito.ru/{city_slug}/{category}/prodam")
    if not url:
        raise ParseError("Ожидалось объявление о продаже выбранного типа недвижимости.")
    soup = html_document(html)
    main = soup.select_one("main") or soup
    status = listing_status([tag.get_text(" ", strip=True) for tag in soup.select(LISTING_STATUS_HEADING_SELECTOR)],
                            [tag.get_text(" ", strip=True) for tag in soup.select(LISTING_STATUS_MESSAGE_SELECTOR)])
    if status == "review":
        raise ListingOnReview("Объявление находится на проверке Авито. Сбор продолжается.")
    if status == "closed":
        raise ListingClosed("Объявление закрыто на Авито. Сбор продолжается.")
    if status == "rejected":
        raise ListingRejected("Объявление отклонено Авито. Сбор продолжается.")
    if status == "deleted":
        raise ListingDeleted("Объявление удалено пользователем. Сбор продолжается.")
    if status == "blocked":
        raise ListingBlocked("Объявление заблокировано Авито после проверки. Сбор продолжается.")
    if listing_is_auction([tag.get_text(" ", strip=True) for tag in main.select("h1")]):
        raise ListingAuction("Объявление с аукционом пропущено. Сбор продолжается.")
    params, product, details = _listing_parameters(main, soup)

    def element_value(selector):
        tag = main.select_one(selector) or soup.select_one(selector)
        return clean(tag.get("content") or tag.get_text(" ", strip=True)) if tag else ""

    title = element_value('h1[data-marker="item-view/title-info"], h1[itemprop="name"], h1') or clean(product.get("name"))
    if listing_is_auction([title], [f"{label}: {value}" for label, value in params.items()]):
        raise ListingAuction("Объявление с аукционом пропущено. Сбор продолжается.")
    if property_kind == 'house':
        for label, code in {**{name.casefold(): code for code, name in HOUSE_FEATURES.items()},
                            'тип объекта': 'object_type', 'готовность дома': 'readiness', 'статус строительства': 'readiness'}.items():
            if label in params:
                params.setdefault(HOUSE_FEATURES[code].casefold(), params[label])
    address = _listing_address(main, soup, product, details, params, None if property_kind == 'house' else city_name)
    market = " ".join(params.get(key, "") for key in ("тип жилья", "тип недвижимости", "рынок"))
    breadcrumbs = element_value('[data-marker="breadcrumbs"], [itemtype="https://schema.org/BreadcrumbList"]')
    market = (market + " " + breadcrumbs).lower()
    market_code = "new" if "новостро" in market else "secondary" if "вторич" in market else None
    if property_kind == 'house' and market_code is None:
        market_code = 'unknown'
    rooms_value = clean(params.get("количество комнат")).lower()
    layout_values = [rooms_value, clean(params.get("планировка")).lower(),
                     clean(params.get("тип планировки")).lower()]
    is_open_plan = (any(re.fullmatch(r"свободн(?:ая|ой)(?:\s+планировк[а-я]*)?", value) for value in layout_values)
                    or bool(re.search(r"\bсвободн(?:ая|ой|ую)\s+планировк[а-я]*\b", title.lower())))
    # A free layout has no room count; zero remains reserved for studios.
    if not rooms_value and not is_open_plan:
        rooms_match = re.search(r"(\d+)\s*-?\s*к[.\s]", title.lower())
        rooms_value = rooms_match[1] if rooms_match else "студия" if "студия" in title.lower() else ""
    rooms = 0 if "студ" in rooms_value.lower() else stored_integer(rooms_value, 'количество комнат') if rooms_value.isdigit() else None
    total = number(params.get("площадь дома") if property_kind == 'house' else params.get("общая площадь"))
    if total is None and (match := re.search(r"(\d+(?:[,.]\d+)?)\s*м²", title)):
        total = number(match[1])
    total = stored_decimal(total, 10, 'общая площадь')
    offers = {}
    for raw in (product.get("offers"), details.get("offers")):
        valid = next((offer for offer in (raw if isinstance(raw, list) else [raw])
                      if isinstance(offer, dict) and offer), None)
        if valid is not None:
            offers = valid
            break
    price_text = element_value('meta[itemprop="price"], [data-marker="item-view/item-price"] [itemprop="price"], [data-marker="item-view/item-price"]') or clean(offers.get("price"))
    price_text = re.sub(r"\s|₽|руб\.?", "", price_text).replace(",", ".")
    price = None
    if re.fullmatch(r"\d+(?:\.0+)?", price_text):
        amount = Decimal(price_text)
        if amount > BIGINT_MAX:
            raise ParseError("Число вне допустимого диапазона: цена.")
        price = int(amount)
    currency = element_value('[itemprop="priceCurrency"]') or clean(offers.get("priceCurrency"))
    if currency and currency.upper() not in {"RUB", "RUR", "₽"}:
        raise ParseError("Цена объявления указана не в рублях.")
    required = {"заголовок": title, "адрес": address, "тип жилья": market_code, "общая площадь": total, "цена": price}
    missing = [key for key, value in required.items() if not value]
    if property_kind == "apartment" and rooms is None and not is_open_plan:
        missing.append("количество комнат")
    if missing:
        raise ParseError("Не удалось прочитать поля: " + ", ".join(missing) + ".")
    floor_text = params.get("этаж", "")
    if not floor_text and (match := re.search(r"(-?\d+)\s*/\s*(\d+)\s*эт", title)):
        floor_text = f"{match[1]} из {match[2]}"
    floor_match = re.fullmatch(r"(-?\d+)\s*(?:из|/)\s*(\d+)", floor_text)
    floor = (stored_integer(floor_match[1], 'этаж') if floor_match else
             stored_integer(floor_text, 'этаж') if re.fullmatch(r"-?\d+", floor_text) else None)
    floors = stored_integer(floor_match[2], 'этажей в доме') if floor_match else None
    if floors is not None and (floors < 1 or floor > floors):
        raise ParseError("Некорректный этаж объявления.")
    if property_kind == 'house':
        floors_text = clean(params.get('этажей в доме'))
        floor, floors = None, (stored_integer(floors_text, 'этажей в доме') or None) if floors_text.isdigit() else None
    features = _listing_features(main, params, property_kind, title, address)
    # A handover date is not a construction year; keep unspecified years empty.
    year_match = re.fullmatch(r"([12]\d{3})(?:\s*(?:г\.?|год))?", params.get("год постройки", ""), re.I)
    construction_year = int(year_match[1]) if year_match else None
    modified = element_value('[itemprop="dateModified"], meta[property="article:modified_time"]')
    if not modified:
        modified = clean(product.get("dateModified") or details.get("dateModified"))
    if not modified:
        date_text = element_value('[data-marker="item-view/item-date"], [data-marker="item-view/item-updated"]')
        if "обновлено" in date_text.lower():
            modified = date_text[date_text.lower().index("обновлено"):].split("·")[0].strip()
    avito_id = re.search(r"_(\d+)$", url)[1]
    latitude, longitude = _listing_coordinates(soup, product, details, avito_id)
    land_area = source_distance = distance = None
    if property_kind == 'house':
        land_area = house_number(params.get('площадь участка'), 'land')
        source_distance = house_number(params.get('расстояние до центра города') or params.get('расстояние до города'), 'distance')
        actual_distance = None
        if city_center is not None and latitude is not None and longitude is not None:
            actual_distance = distance_km(city_center, (latitude, longitude))
            distance = Decimal(str(actual_distance)).quantize(Decimal('.01'))
        if radius_km is not None:
            if distance is None:
                raise ListingSkipped('Нет координат дома для проверки радиуса от центра города. Объявление пропущено.')
            if actual_distance > float(radius_km) + 1e-8:
                raise OutsideCity(f'Дом находится в {distance} км от центра {city_name}, за пределами радиуса {radius_km} км.')
    return ListingData(
        property_kind=property_kind, land_area=land_area,
        distance_to_center_km=distance, source_distance_km=source_distance,
        avito_id=avito_id, url=url, title=title,
        market_type_code=market_code, address=address, rooms=rooms, total_area=total, price=price,
        is_open_plan=is_open_plan,
        latitude=latitude, longitude=longitude,
        complex_name=params.get("название жк") or params.get("жилой комплекс") or params.get("название новостройки") or element_value('[data-marker="item-view/realty/development-name"]') or None,
        kitchen_area=stored_decimal(number(params.get("площадь кухни")), 10, 'площадь кухни'),
        living_area=stored_decimal(number(params.get("жилая площадь")), 10, 'жилая площадь'),
        floor=floor, floors_total=floors,
        ceiling_height=stored_decimal(number(params.get("высота потолков")), 6, 'высота потолков'),
        construction_year=construction_year,
        source_updated_at=parse_source_date(modified, now), features=features,
    )
