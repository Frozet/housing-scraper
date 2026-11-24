"""Selectors shared by the HTML parser and the optional local browser helper."""
import re

LISTING_STATUS_HEADING_SELECTOR = "h1"
LISTING_STATUS_MESSAGE_SELECTOR = "h1 + p"
LISTING_PARAM_ROWS_SELECTOR = (
    '[data-marker="item-view/item-params"] li, [data-marker="item-view/params"] li, '
    'li[class*="params-paramsList"], [data-marker="item-view/item-params"] p'
)


def listing_status(headings, messages):
    def normalized(value):
        return re.sub(r"\s+", " ", value).strip().casefold().replace("ё", "е").rstrip(".!…")
    # Match the listing's status message, not a mention in a description or a CAPTCHA.
    if not any(normalized(value) == "объявление не посмотреть" for value in headings):
        return None
    statuses = {
        "оно еще на проверке": "review", "объявление закрыто": "closed", "объявление истекло": "closed",
        "оно отклонено, но снова появится в поиске, если его исправят": "rejected",
        "пользователь его удалил": "deleted",
        "оно заблокировано после проверки": "blocked",
    }
    return next((statuses[normalized(value)] for value in messages if normalized(value) in statuses), None)


def listing_is_auction(headings, parameter_rows=()):
    # Use the actual title/labelled sale terms, not arbitrary mentions in a
    # description, recommendation or scripts bundled into the page.
    if any(re.match(r"^\s*аукцион\s*(?:[:：—–-]|$)", value, re.I) for value in headings):
        return True
    for row in parameter_rows:
        label, separator, value = re.sub(r"\s+", " ", row).strip().casefold().partition(":")
        if separator and label.strip() in {"условия продажи", "способ продажи"}:
            if any(term.strip().rstrip(".!…") == "аукцион" for term in re.split(r"[,;]", value)):
                return True
    return False

# Keep narrow listing markers ahead of layout classes and individual address parts.
ADDRESS_SELECTORS = (
    # The current layout uses hashed classes. The first span after this heading is
    # the address; the following spans contain the district and map controls.
    '#item-view-address h2 + div span',
    '[data-marker="item-view/item-address"]',
    '[data-marker="item-view/address"]',
    '[data-marker="item-view/location"]',
    '[itemprop~="address"]',
    '[class*="item-address__string"]',
    '.item-address',
    '[class*="geo-address"]',
    '[class*="style-item-address"]',
    '[class*="style-address"]',
    '[itemprop~="streetAddress"]',
)
ADDRESS_SELECTOR = ", ".join(ADDRESS_SELECTORS)
