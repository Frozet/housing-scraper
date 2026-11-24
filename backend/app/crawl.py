"""Durable, deduplicated queue and adaptive price-range traversal."""
import hashlib
import re
from urllib.parse import urlsplit

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.browser_jobs import RUN_START_LOCK, expire_browser_runs
from app.config import settings
from app.models import Crawl, CrawlItem, CrawlPage, CrawlRange, CollectionRun, Listing, SamplePlan, utcnow
from app.cities import city_catalog, listing_city_condition, selected_city
from app.price_ranges import price_bounds, radius_bounds, range_label, range_url, split_bounds
from app.repository import create_run
from app.property_types import house_radius
from app.catalog_navigation import catalog_url, page_in_url
from app.sampling import QUOTA

# The user's catalog stops here even when its counter reports many more items.
CATALOG_PAGE_LIMIT = 30


def current_range(session, crawl):
    return session.scalar(select(CrawlRange).where(CrawlRange.crawl_id == crawl.id,
        CrawlRange.status.in_(["active", "pending"]))
        .order_by((CrawlRange.status == "active").desc(), CrawlRange.minimum, CrawlRange.number).limit(1))


def start_browser_run(session, fresh=False, city_id=None, collection_mode="full", property_kind="apartment"):
    session.execute(select(func.pg_advisory_xact_lock(RUN_START_LOCK)))
    expire_browser_runs(session)
    if session.scalar(select(CollectionRun.id).where(CollectionRun.status.in_(["queued", "running"])).limit(1)):
        raise ValueError("Сбор уже запущен. Дождитесь его завершения.")
    city = selected_city(session, city_id)
    crawl = session.scalar(select(Crawl).where(Crawl.city_id == city.id, Crawl.mode == collection_mode, Crawl.property_kind == property_kind)
                           .order_by(Crawl.id.desc()).limit(1).with_for_update())
    errors = crawl and session.scalar(select(func.count()).select_from(CrawlItem).where(
        CrawlItem.crawl_id == crawl.id, CrawlItem.status == "error"))
    resumable = crawl and (crawl.status == "active" or (crawl.status == "completed" and errors))
    base = city_catalog(city, property_kind)
    if resumable and not fresh and urlsplit(crawl.catalog_url).path != urlsplit(base).path:
        raise ValueError("Адрес каталога изменён. Для нового адреса нажмите «Новый обход с начала» "
                         "или запустите новую выборку либо обновление. Старая очередь сохранена.")
    # Query parameters (including house radius) belong to the saved queue.
    if fresh or not resumable:
        if crawl and crawl.status == "active":
            crawl.status, crawl.finished_at = "abandoned", utcnow()
            session.flush()
        bootstrap = collection_mode == "full" and not fresh and crawl is None and session.scalar(
            select(Listing.id).where(listing_city_condition(city.id), Listing.property_kind == property_kind).limit(1)) is not None
        crawl = Crawl(catalog_url=base, city_id=city.id, mode=collection_mode,
                      property_kind=property_kind, radius_km=house_radius(city) if property_kind == 'house' else None,
                      skip_existing_before=utcnow() if bootstrap else None)
        session.add(crawl)
        session.flush()
        if collection_mode == "sample":
            session.add(SamplePlan(crawl_id=crawl.id))
        else:
            session.add(CrawlRange(crawl_id=crawl.id, number=1, minimum=1, maximum=None))
            if collection_mode == "refresh":
                session.flush()
                rows = session.execute(select(Listing.avito_id, Listing.url).where(
                    listing_city_condition(city.id), Listing.property_kind == property_kind, Listing.is_archived.is_(False)).order_by(Listing.id)).all()
                session.add(CrawlPage(crawl_id=crawl.id, range_number=1, number=1,
                    url=catalog_url(crawl.catalog_url, 1), has_next=False,
                    fingerprint=hashlib.sha256('\n'.join(row.avito_id for row in rows).encode()).hexdigest()))
                session.flush()
                session.add_all([CrawlItem(crawl_id=crawl.id, avito_id=row.avito_id, url=row.url,
                    range_number=1, page_number=1, position=position) for position, row in enumerate(rows)])
    else:
        crawl.status, crawl.finished_at = "active", None
        session.execute(update(CrawlItem).where(CrawlItem.crawl_id == crawl.id, CrawlItem.status == "error")
                        .values(status="pending", read_attempts=0))
        if collection_mode == "sample":
            session.execute(update(CrawlRange).where(CrawlRange.crawl_id == crawl.id,
                CrawlRange.status.in_(["completed", "limited"]), CrawlRange.number.in_(select(CrawlItem.range_number).where(
                    CrawlItem.crawl_id == crawl.id, CrawlItem.status == "pending"))).values(status="pending"))
    run = create_run(session)
    run.crawl_id = crawl.id
    if collection_mode == "refresh":
        run.found = session.scalar(select(func.count()).select_from(CrawlItem).where(CrawlItem.crawl_id == crawl.id))
    run.message = "Ожидается локальный сборщик. Прогресс обхода сохранён в базе."
    session.flush()
    return run


def progress(session, city_id=None, property_kind="apartment", mode=None):
    query = select(Crawl).where(Crawl.property_kind == property_kind)
    if city_id is not None:
        query = query.where(Crawl.city_id == city_id)
    if mode is not None:
        query = query.where(Crawl.mode == mode)
    crawl = session.scalar(query.order_by(Crawl.id.desc()).limit(1))
    if crawl is None:
        existing = session.scalar(select(func.count(Listing.id)).where(Listing.property_kind == property_kind, *([listing_city_condition(city_id)] if city_id else [])))
        return {"crawl_id": None, "can_resume": bool(existing), "legacy_count": existing}
    counts = dict(session.execute(select(CrawlItem.status, func.count()).where(CrawlItem.crawl_id == crawl.id)
                                  .group_by(CrawlItem.status)).all())
    ranges = dict(session.execute(select(CrawlRange.status, func.count()).where(CrawlRange.crawl_id == crawl.id)
                                  .group_by(CrawlRange.status)).all())
    active = current_range(session, crawl)
    sample = None
    if crawl.mode == "sample":
        plan = session.get(SamplePlan, crawl.id)
        saved_bands = dict(session.execute(select(CrawlItem.range_number, func.count()).where(
            CrawlItem.crawl_id == crawl.id, CrawlItem.status == "saved").group_by(CrawlItem.range_number)).all())
        bands = {row.number: row for row in session.scalars(select(CrawlRange).where(CrawlRange.crawl_id == crawl.id))}
        sample = {"phase": plan.phase, "population": plan.total_count,
                  "groups": [{"number": n, "saved": saved_bands.get(n, 0), "target": QUOTA,
                              "minimum": bands[n].minimum if n in bands else None,
                              "maximum": bands[n].maximum if n in bands else None,
                              "status": bands[n].status if n in bands else None} for n in range(1, 11)]}
    return {"crawl_id": crawl.id, "city_id": crawl.city_id, "mode": crawl.mode, "sample": sample,
            "status": crawl.status, "radius_km": crawl.radius_km, "next_page": active.next_page if active else None,
            "range": {"number": active.number, "minimum": active.minimum, "maximum": active.maximum,
                      "label": range_label(active.minimum, active.maximum)} if active else None,
            "ranges_completed": ranges.get("completed", 0),
            "ranges_remaining": ranges.get("pending", 0) + ranges.get("active", 0),
            "ranges_limited": ranges.get("limited", 0),
            "can_resume": crawl.status == "active" or (crawl.status == "completed" and counts.get("error", 0) > 0),
            "queued": counts.get("pending", 0), "saved": counts.get("saved", 0),
            "skipped": counts.get("skipped", 0), "errors": counts.get("error", 0),
            "discovered": sum(counts.values()), "legacy_count": 0}


def resumable_modes(session, city_id, property_kind):
    newest = (select(Crawl.id, Crawl.mode, Crawl.status).where(
        Crawl.city_id == city_id, Crawl.property_kind == property_kind)
        .distinct(Crawl.mode).order_by(Crawl.mode, Crawl.id.desc()).subquery())
    errors = select(CrawlItem.avito_id).where(CrawlItem.crawl_id == newest.c.id, CrawlItem.status == 'error').exists()
    return [dict(id=row.id, mode=row.mode) for row in session.execute(select(newest.c.id, newest.c.mode).where(
        (newest.c.status == 'active') | ((newest.c.status == 'completed') & errors)).order_by(newest.c.id.desc()))]


def divide_range(session, part):
    children = split_bounds(part.minimum, part.maximum)
    if children is None:
        part.status = "limited"
        session.flush()
        return
    number = session.scalar(select(func.max(CrawlRange.number)).where(CrawlRange.crawl_id == part.crawl_id))
    part.status = "split"
    session.flush()  # Release the unique active-range slot before selecting a child.
    for offset, (low, high) in enumerate(children, 1):
        session.add(CrawlRange(crawl_id=part.crawl_id, number=number + offset, parent_number=part.number,
                               minimum=low, maximum=high, status="pending"))
    session.flush()


def recover_catalog_range(session, run, requested_url, page_number, range_number, actual_url):
    crawl = session.get(Crawl, run.crawl_id)
    if crawl is None or crawl.mode != 'full' or crawl.status != 'active':
        raise ValueError('Деление недоступной выдачи поддерживается только для полного обхода.')
    part = current_range(session, crawl)
    if (part is None or part.status != 'active' or part.number != range_number
            or part.next_page != page_number or page_number <= 1 or page_in_url(requested_url) != page_number):
        raise ValueError('Получена другая страница или диапазон. Прогресс сохранён.')
    expected = (part.minimum, part.maximum) if part.parent_number is not None else None
    if any(price_bounds(url) != expected or radius_bounds(url) != radius_bounds(crawl.catalog_url)
           for url in (requested_url, actual_url)):
        raise ValueError('Авито изменил фильтр цены или радиуса. Прогресс сохранён.')
    if session.scalar(select(CrawlItem.avito_id).where(
            CrawlItem.crawl_id == crawl.id, CrawlItem.status == 'pending').limit(1)) is not None:
        raise ValueError('Сначала необходимо обработать сохранённую очередь объявлений.')
    label = range_label(part.minimum, part.maximum)
    divide_range(session, part)
    action = 'split' if part.status == 'split' else 'limited'
    run.message = (f'Авито вернул первую страницу вместо {page_number}-й. Диапазон {label} '
                   + ('разделён на более узкие; сбор продолжается.' if action == 'split' else
                      'содержит одну цену; полнота этого диапазона не подтверждена. Сбор других диапазонов продолжается.'))
    return {'action': action, 'message': run.message}


def run_budget(session, crawl, run):
    # Limit accepted catalog/probe responses and finalized items; retries of a card share one item.
    config = settings()
    processed = session.scalar(select(func.count()).select_from(CrawlItem).where(
        CrawlItem.crawl_id == crawl.id, CrawlItem.last_run_id == run.id,
        CrawlItem.status != "pending")) if config.max_listings else 0
    limit = ("объявлений" if config.max_listings and processed >= config.max_listings else None)
    pages = ("страниц" if config.max_pages and run.pages >= config.max_pages else None)
    return limit, pages


def limit_reached(resource):
    return {"kind": "done", "status": "partial", "message": f"Достигнут лимит {resource}. Прогресс сохранён."}


def listing_task(item):
    task = {"kind": "listing", "url": item.url}
    if item.read_attempts:
        task["read_attempts"] = item.read_attempts
    return task


def catalog_task(session, crawl, part, base):
    task = {"kind": "catalog", "url": catalog_url(base, part.next_page),
            "page_number": part.next_page, "range_number": part.number}
    previous = session.get(CrawlPage, (crawl.id, part.number, part.next_page - 1))
    if previous:
        task["previous_url"] = previous.url
    return task


def next_task(session, run):
    crawl = session.get(Crawl, run.crawl_id)
    if crawl is None:
        raise ValueError("Перезапустите сборщик: текущий запуск создан старой версией приложения.")
    limited, pages_limited = run_budget(session, crawl, run)
    if crawl.mode == "sample":
        from app.sampling import sample_next_task
        return sample_next_task(session, crawl, run, limited, pages_limited)
    item = session.scalar(select(CrawlItem).where(CrawlItem.crawl_id == crawl.id, CrawlItem.status == "pending")
                          .order_by(CrawlItem.range_number, CrawlItem.page_number, CrawlItem.position).limit(1))
    if item:
        if limited:
            return limit_reached(limited)
        return listing_task(item)
    if crawl.mode == "refresh":
        errors = session.scalar(select(func.count()).select_from(CrawlItem).where(
            CrawlItem.crawl_id == crawl.id, CrawlItem.status == 'error'))
        for part in session.scalars(select(CrawlRange).where(CrawlRange.crawl_id == crawl.id)):
            part.status = 'completed'
        crawl.status, crawl.finished_at = 'completed', utcnow()
        return {'kind': 'done', 'status': 'partial' if errors else 'completed',
                'message': f'Обновление сохранённых объявлений завершено. Ошибок чтения: {errors}.'}
    while (part := current_range(session, crawl)) is not None:
        part.status = "active"
        previous = session.get(CrawlPage, (crawl.id, part.number, part.next_page))
        if previous is None and part.next_page > CATALOG_PAGE_LIMIT:
            previous = session.get(CrawlPage, (crawl.id, part.number, CATALOG_PAGE_LIMIT))
        if previous:
            # An apparent end on page 30 only completes this query, not the city.
            if previous.number >= CATALOG_PAGE_LIMIT and previous.fingerprint != hashlib.sha256(b"").hexdigest():
                divide_range(session, part)
                continue
            if previous.has_next is False:
                part.status = "completed"
                session.flush()
                continue
            part.next_page += 1
        if limited or pages_limited:
            return limit_reached(limited or pages_limited)
        base = range_url(crawl.catalog_url, part.minimum, part.maximum, filtered=part.parent_number is not None)
        run.message = f"Диапазон {range_label(part.minimum, part.maximum)} · страница {part.next_page}."
        return catalog_task(session, crawl, part, base)
    errors = session.scalar(select(func.count()).select_from(CrawlItem).where(
        CrawlItem.crawl_id == crawl.id, CrawlItem.status == "error"))
    unresolved = session.scalar(select(func.count()).select_from(CrawlRange).where(
        CrawlRange.crawl_id == crawl.id, CrawlRange.status == "limited"))
    if unresolved:
        return {"kind": "done", "status": "partial", "message":
                f"Осталось диапазонов с одной ценой и ограниченной выдачей: {unresolved}. Полнота сбора не подтверждена."}
    crawl.status, crawl.finished_at = "completed", utcnow()
    return {"kind": "done", "status": "partial" if errors else "completed", "message":
            f"Диапазоны цен пройдены. Не удалось прочитать объявлений: {errors}." if errors else
            "Все диапазоны цен пройдены. Число объявлений на Авито может меняться во время сбора."}


def store_catalog(session, run, url, page_number, urls, has_next, recheck=False, range_number=1):
    crawl = session.get(Crawl, run.crawl_id)
    part = session.get(CrawlRange, (crawl.id, range_number)) if crawl else None
    active = current_range(session, crawl) if crawl else None
    if part is None or active is None or part.number != active.number:
        raise ValueError("Получен другой диапазон цен. Перезапустите сборщик; прогресс сохранён.")
    expected = (part.minimum, part.maximum) if part.parent_number is not None or crawl.mode == "sample" else None
    if price_bounds(url) != expected or radius_bounds(url) != radius_bounds(crawl.catalog_url):
        raise ValueError("Авито открыл каталог с другим фильтром цены. Полнота сбора не подтверждена; прогресс сохранён.")
    page = session.get(CrawlPage, (crawl.id, part.number, page_number))
    pending = session.scalar(select(CrawlItem.avito_id).where(
        CrawlItem.crawl_id == crawl.id, CrawlItem.status == "pending").limit(1)) if recheck else None
    valid_recheck = crawl.status == "active" and recheck and page and page_number == part.next_page - 1 and pending is None
    if (recheck and not valid_recheck) or (not recheck and page_number != part.next_page):
        raise ValueError("Получена другая страница каталога. Перезапустите сборщик; прогресс сохранён.")
    if page and not recheck:
        return
    fingerprint = hashlib.sha256("\n".join(sorted(urls)).encode()).hexdigest()
    if urls and session.scalar(select(CrawlPage.number).where(
            CrawlPage.crawl_id == crawl.id, CrawlPage.range_number == part.number, CrawlPage.number != page_number,
            CrawlPage.fingerprint == fingerprint).limit(1)) is not None:
        raise ValueError("Авито повторно показал уже обработанную страницу. Полнота обхода не подтверждена; прогресс сохранён.")
    if page:
        page.url, page.fingerprint, page.has_next, page.received_at = url, fingerprint, has_next, utcnow()
        part.next_page = page_number
    else:
        session.add(CrawlPage(crawl_id=crawl.id, range_number=part.number, number=page_number,
                              url=url, fingerprint=fingerprint, has_next=has_next))
    part.status = "active"
    session.flush()
    candidates = {}
    for position, item_url in enumerate(urls):
        avito_id = re.search(r"_(\d+)$", item_url)[1]
        candidates.setdefault(avito_id, (position, item_url))
    existing = set()
    if candidates and crawl.skip_existing_before:
        existing = set(session.scalars(select(Listing.avito_id).where(
            Listing.avito_id.in_(candidates), Listing.last_seen_at <= crawl.skip_existing_before,
            Listing.property_kind == crawl.property_kind, listing_city_condition(crawl.city_id))))
    added = 0
    if candidates:
        now = utcnow()
        statement = insert(CrawlItem).values([
            dict(crawl_id=crawl.id, range_number=part.number, avito_id=avito_id, url=item_url,
                 page_number=page_number, position=position, updated_at=now,
                 status="saved" if avito_id in existing else "pending")
            for avito_id, (position, item_url) in candidates.items()
        ])
        if crawl.mode == "sample":
            statement = statement.on_conflict_do_update(index_elements=[CrawlItem.crawl_id, CrawlItem.avito_id],
                set_={key: getattr(statement.excluded, key) for key in
                      ("range_number", "page_number", "position", "url", "status", "updated_at")}
                      | {"error": None, "read_attempts": 0},
                where=CrawlItem.status == "not_selected")
        else:
            statement = statement.on_conflict_do_nothing()
        added = len(session.scalars(statement.returning(CrawlItem.avito_id)).all())
    run.pages += 1
    run.found += added
    run.message = f"Диапазон {range_label(part.minimum, part.maximum)} · страница {page_number}: очередь сохранена."
