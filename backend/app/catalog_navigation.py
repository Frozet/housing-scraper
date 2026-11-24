"""Catalog page identity shared by the local helper and the server parser."""
import re
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

CURRENT_PAGE_SELECTOR = (
    '[aria-current="page"], '
    '[data-marker^="pagination-button"][aria-current="true"], '
    '[data-marker^="pagination-button"][aria-disabled="true"], '
    '[data-marker^="pagination-button"][disabled], '
    '[data-marker="pagination-button/current"]'
)


def catalog_url(base, number, *, keep_blank_values=True):
    """Set the page without dropping the price, radius or other catalog filters."""
    parts = urlsplit(base)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=keep_blank_values)
             if key != "p"]
    return urlunsplit(parts._replace(query=urlencode(query + [("p", str(number))])))


def rendered_page(texts):
    pages = {int(text.strip()) for text in texts if re.fullmatch(r"[0-9]+", text.strip()) and int(text.strip()) > 0}
    return next(iter(pages)) if len(pages) == 1 else None


def page_in_url(url):
    values = parse_qs(urlsplit(url).query, keep_blank_values=True).get("p", ["1"])
    return rendered_page(values) if all(value.isascii() and value.isdigit() and int(value) > 0 for value in values) else None


def catalog_page(url, current_texts):
    # An explicit current-page marker describes the rendered content even when
    # the site's router rewrites or temporarily drops the query parameter.
    numeric = [text for text in current_texts if re.fullmatch(r"[0-9]+", text.strip()) and int(text.strip()) > 0]
    return rendered_page(numeric) if numeric else page_in_url(url)
