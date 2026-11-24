"""Запуск: python -m app.collect [--html файл.html --url https://www.avito.ru/...]."""
import argparse
import json
from pathlib import Path

from sqlalchemy.exc import IntegrityError

from app.collector import run_once
from app.config import settings
from app.crawl import start_browser_run
from app.db import SessionLocal
from app.parser import ListingSkipped, parse_listing
from app.repository import create_run, save_listing


def main():
    parser = argparse.ArgumentParser(description="Сбор квартир и домов с сохранением истории цен.")
    parser.add_argument("--html", type=Path, help="Импорт сохранённого HTML объявления")
    parser.add_argument("--url", help="Исходный адрес объявления; обязателен при --html")
    parser.add_argument("--new", action="store_true", help="Новый обход с начала для обновления цен")
    parser.add_argument("--property-kind", choices=["apartment", "house"], default="apartment", help="Рынок недвижимости")
    parser.add_argument("--city-id", type=int, help="Город из справочника приложения")
    args = parser.parse_args()
    if bool(args.html) != bool(args.url):
        parser.error("Укажите --html и --url вместе.")
    if args.html:
        try:
            source = args.html.read_text(encoding="utf-8")
            if args.property_kind == 'house' or args.city_id is not None:
                from app.cities import selected_city
                from app.property_types import house_radius
                with SessionLocal() as context:
                    city = selected_city(context, args.city_id)
                    data = parse_listing(source, args.url, property_kind=args.property_kind, city_name=city.name,
                                         city_slug=city.slug, city_center=(city.latitude, city.longitude) if city.latitude is not None else None,
                                         radius_km=house_radius(city) if args.property_kind == 'house' else None)
            else:
                data = parse_listing(source, args.url)
        except ListingSkipped as exc:
            print(json.dumps({"сохранено": False, "причина": str(exc)}, ensure_ascii=False))
            return
        with SessionLocal.begin() as session:
            listing, changed = save_listing(session, data, city_id=args.city_id)
            print(json.dumps({"id": listing.id, "изменение_цены": changed}, ensure_ascii=False))
        return
    if settings().collector_mode != 'browser' and (args.property_kind != 'apartment' or args.city_id is not None):
        parser.exit(1, 'Для выбора рынка и города включите локальный браузерный режим.\n')
    try:
        with SessionLocal.begin() as session:
            run = start_browser_run(session, fresh=args.new, city_id=args.city_id, property_kind=args.property_kind) if settings().collector_mode == "browser" else create_run(session)
            run_id = run.id
    except (IntegrityError, ValueError):
        parser.exit(1, "Сбор уже запущен. Дождитесь его завершения.\n")
    if settings().collector_mode == "browser":
        print("Задание поставлено в очередь. Откройте файл «Запустить сборщик.command» для работы с локальным браузером.")
    else:
        run_once()
    with SessionLocal() as session:
        from app.models import CollectionRun
        run = session.get(CollectionRun, run_id)
        print(json.dumps({"id": run.id, "статус": run.status, "сообщение": run.message,
                          "сохранено": run.saved, "изменений_цены": run.price_changes}, ensure_ascii=False))
        if run.status in {"blocked", "failed", "interrupted"}:
            parser.exit(1)


if __name__ == "__main__":
    main()
