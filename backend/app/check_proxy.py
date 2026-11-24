"""Проверка выбранного прокси: python -m app.check_proxy. Реквизиты не выводятся."""
import ipaddress
import json
import re
import threading

import httpx

from app.collector import CollectionError, Fetcher
from app.config import Settings, settings
from app.parser import CATALOG_URL, ParseError, parse_catalog

COUNTRY_URL = "https://api.country.is/"


def check_proxy(config: Settings | None = None) -> dict:
    config = config or settings()
    result = {"ok": False, "proxy_configured": config.avito_proxy_url is not None}
    if not result["proxy_configured"]:
        return {**result, "message": "Прокси не настроен. Укажите AVITO_PROXY_URL в .env."}
    fetcher = None
    try:
        fetcher = Fetcher(config, threading.Event())
        try:
            response = fetcher.client.get(COUNTRY_URL)
            response.raise_for_status()
            payload = response.json()
            ip = str(ipaddress.ip_address(payload["ip"]))
            country = payload["country"]
            if not isinstance(country, str) or not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise CollectionError("Не удалось определить внешний IP через прокси. Проверьте доступность прокси и сервиса country.is.") from None
        result.update(exit_ip=ip, country=country)
        if country != "RU":
            return {**result, "message": "GeoIP определяет выходной адрес вне России. Проверьте выбранную страну у провайдера прокси."}
        html = fetcher.get(CATALOG_URL + "?p=1")
        urls, has_next = parse_catalog(html)
        return {**result, "ok": True, "listings_found": len(urls), "has_next": has_next,
                "message": "Прокси работает, GeoIP указывает Россию; каталог Авито распознан."}
    except (CollectionError, ParseError) as exc:
        return {**result, "message": str(exc)}
    finally:
        if fetcher is not None:
            fetcher.close()


def main():
    result = check_proxy()
    print(json.dumps(result, ensure_ascii=False))
    raise SystemExit(0 if result["ok"] else 1)


if __name__ == "__main__":
    main()
