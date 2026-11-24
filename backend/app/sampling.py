"""Durable price deciles from catalog counters; at most 100 saved listings per band."""
from sqlalchemy import func, select, update

from app.models import CrawlItem, CrawlPage, CrawlRange, SampleBoundary, SamplePlan, SampleProbe, utcnow
from app.price_ranges import MAX_PRICE, price_bounds, radius_bounds, range_url

QUOTA = 100
# Catalog counters are live estimates, not a transactionally consistent snapshot.
# A 10,000-ruble grid avoids dozens of requests differing by a single ruble.
PRICE_STEP = 10_000
MAX_PROBE_ATTEMPTS = 3


def counter_tolerance(population):
    return max(2, (population + 399) // 400)  # 0.25% of the city, at least two listings.


def probe_task(crawl, maximum):
    return {"kind": "catalog", "purpose": "sample_probe", "page_number": 1, "maximum": maximum,
            "url": range_url(crawl.catalog_url, 1, maximum, filtered=maximum is not None)}


def prepare_sample(session, crawl, run):
    plan = session.get(SamplePlan, crawl.id)
    if plan.phase == "count":
        run.message = "Выборка 1000: определяется число объявлений города."
        return probe_task(crawl, None)
    if plan.total_count == 0:
        return None
    while plan.phase == "expand":
        probe = session.get(SampleProbe, (crawl.id, plan.upper_price))
        if probe is None:
            run.message = "Выборка 1000: определяется верхняя граница ценовых групп."
            return probe_task(crawl, plan.upper_price)
        if probe.count * 10 >= plan.total_count * 9:
            for number in range(1, 10):
                session.add(SampleBoundary(crawl_id=crawl.id, number=number, low=1, high=plan.upper_price))
            plan.phase = "boundaries"
            session.flush()
        elif plan.upper_price == MAX_PRICE:
            raise ValueError("Счётчики Авито не согласуются. Запустите новую выборку.")
        else:
            plan.upper_price = min(MAX_PRICE, plan.upper_price * 2)
    if plan.phase == "boundaries":
        boundaries = session.scalars(select(SampleBoundary).where(SampleBoundary.crawl_id == crawl.id)
                                     .order_by(SampleBoundary.number)).all()
        for boundary in boundaries:
            target = (plan.total_count * boundary.number + 9) // 10
            while boundary.price is None:
                if boundary.high - boundary.low < PRICE_STEP:
                    boundary.price = boundary.high
                    break
                middle = max(boundary.low, (boundary.low + boundary.high) // (2 * PRICE_STEP) * PRICE_STEP)
                probe = session.get(SampleProbe, (crawl.id, middle))
                if probe is None:
                    run.message = f"Выборка 1000: определяется граница {boundary.number * 10}% по ценам."
                    return probe_task(crawl, middle)
                if probe.count >= target:
                    boundary.high = middle
                else:
                    boundary.low = middle + 1
        prices = [row.price for row in boundaries]
        # Inclusive boundaries intentionally share tied prices; the queue deduplicates IDs.
        for number in range(1, 11):
            session.add(CrawlRange(crawl_id=crawl.id, number=number, minimum=1 if number == 1 else prices[number - 2],
                                   maximum=prices[number - 1] if number < 10 else None, status="pending"))
        plan.phase = "collect"
        session.flush()
    return None


def store_probe(session, crawl, run, url, maximum, count):
    plan = session.get(SamplePlan, crawl.id)
    expected = prepare_sample(session, crawl, run)
    if expected is None or expected.get("maximum") != maximum:
        raise ValueError("Получен другой запрос определения ценовых групп. Прогресс сохранён.")
    if price_bounds(url) != ((1, maximum) if maximum is not None else None) or radius_bounds(url) != radius_bounds(crawl.catalog_url):
        raise ValueError("Авито изменил фильтр при определении ценовых групп. Прогресс сохранён.")
    if maximum is None:
        plan.total_count, plan.phase = count, "expand"
    else:
        smaller = session.scalar(select(func.max(SampleProbe.count)).where(SampleProbe.crawl_id == crawl.id, SampleProbe.maximum < maximum))
        larger = session.scalar(select(func.min(SampleProbe.count)).where(SampleProbe.crawl_id == crawl.id, SampleProbe.maximum > maximum))
        low, high = smaller or 0, min(plan.total_count, larger if larger is not None else plan.total_count)
        adjusted = min(high, max(low, count))
        if low > high or abs(adjusted - count) > counter_tolerance(plan.total_count):
            if plan.probe_retries + 1 >= MAX_PROBE_ATTEMPTS:
                raise ValueError("Авито несколько раз вернул сильно различающиеся счётчики. "
                                 "Расчёт ценовых групп приостановлен; прогресс сохранён. "
                                 "Повторите «Продолжить сбор» позже.")
            # Keep the suspect count out of the cache. The next task repeats
            # this price filter through the normal collector delay.
            plan.probe_retries += 1
            run.pages += 1
            run.message = ("Авито изменил счётчик объявлений. Перепроверяется та же ценовая граница: "
                           f"попытка {plan.probe_retries + 1}/{MAX_PROBE_ATTEMPTS}.")
            session.flush()
            return False
        session.add(SampleProbe(crawl_id=crawl.id, maximum=maximum, count=adjusted))
    plan.probe_retries = 0
    run.pages += 1
    session.flush()
    return True


def sample_next_task(session, crawl, run, limited=None, pages_limited=None):
    from app.crawl import CATALOG_PAGE_LIMIT, catalog_task, current_range, limit_reached, listing_task
    task = prepare_sample(session, crawl, run)
    if task:
        return limit_reached(limited or pages_limited) if limited or pages_limited else task
    while (part := current_range(session, crawl)) is not None:
        part.status = "active"
        saved = session.scalar(select(func.count()).select_from(CrawlItem).where(
            CrawlItem.crawl_id == crawl.id, CrawlItem.range_number == part.number, CrawlItem.status == "saved"))
        if saved >= QUOTA:
            changed = session.execute(update(CrawlItem).where(CrawlItem.crawl_id == crawl.id,
                CrawlItem.range_number == part.number, CrawlItem.status.in_(["pending", "error"]))
                .values(status="not_selected", error=None))
            if changed.rowcount:
                run.queue_revision += 1
            part.status = "completed"
            session.flush()
            continue
        item = session.scalar(select(CrawlItem).where(CrawlItem.crawl_id == crawl.id,
            CrawlItem.range_number == part.number, CrawlItem.status == "pending")
            .order_by(CrawlItem.page_number, CrawlItem.position).limit(1))
        run.message = f"Выборка · {10 * (part.number - 1)}–{10 * part.number}% цен · сохранено {saved}/{QUOTA}."
        if item:
            return limit_reached(limited) if limited else listing_task(item)
        previous = session.get(CrawlPage, (crawl.id, part.number, part.next_page))
        if previous:
            if previous.has_next is False or previous.number >= CATALOG_PAGE_LIMIT:
                part.status = "completed" if previous.has_next is False else "limited"
                session.flush()
                continue
            part.next_page += 1
        if limited or pages_limited:
            return limit_reached(limited or pages_limited)
        return catalog_task(session, crawl, part, range_url(crawl.catalog_url, part.minimum, part.maximum))
    counts = dict(session.execute(select(CrawlItem.range_number, func.count()).where(
        CrawlItem.crawl_id == crawl.id, CrawlItem.status == "saved").group_by(CrawlItem.range_number)).all())
    total = sum(counts.values())
    crawl.status, crawl.finished_at = "completed", utcnow()
    complete = all(counts.get(number, 0) == QUOTA for number in range(1, 11))
    message = "Выборка завершена: 1000 объявлений, по 100 в каждой ценовой группе." if complete else (
        f"Выборка завершена частично: {total}/1000. В части групп не хватило доступных объявлений; квоты не перераспределялись.")
    return {"kind": "done", "status": "completed" if complete else "partial", "message": message}
