"""Transactional outcomes for a single queued listing."""
import logging

from app.browser_protocol import MAX_LISTING_READ_ATTEMPTS
from app.listing_history import archive_listing
from app.models import utcnow

log = logging.getLogger(__name__)


def finish_item(run, item, status, reason=None):
    item.status, item.error = status, reason
    item.last_run_id, item.updated_at = run.id, utcnow()
    run.queue_revision += 1
    if status == 'error':
        run.errors += 1
        run.message = f"Пропущено объявление: {reason}"
    elif reason:
        run.message = reason
    if reason:
        log.info("run=%s listing=%s stage=store outcome=%s attempts=%s reason=%s",
                 run.id, item.avito_id, status, item.read_attempts, reason)
    return {"saved": False, "message": run.message}


def archive_item(session, run, item, reason, observed_at=None):
    if archive_listing(session, item.avito_id, reason, observed_at):
        run.archived += 1
        reason = 'Объявление недоступно. Перенесено в архив.'
    return finish_item(run, item, 'skipped', reason)


def record_read_failure(run, item, attempt, reason):
    if item.status == 'pending' and attempt == item.read_attempts + 1:
        item.read_attempts = attempt
        if attempt >= MAX_LISTING_READ_ATTEMPTS:
            return {**finish_item(run, item, 'error', reason), "retry": False}
        item.last_run_id, item.updated_at, item.error = run.id, utcnow(), reason
        run.message = f"Карточка №{item.avito_id}: {reason} Повторная попытка."
    # The attempt number makes retrying after a lost API response safe.
    return {"retry": item.status == 'pending', "message": run.message}
