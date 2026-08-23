from django.db import IntegrityError, transaction

from accounts.models import ProcessedUpdate

DUPLICATE = object()


def process_once(update_id, fn, *args, **kwargs):
    """Run fn(*args, **kwargs) atomically, guarded by a per-update-id dedupe
    marker. If update_id was already processed, returns DUPLICATE without
    calling fn. If fn raises, the marker is rolled back too (safe to retry)."""
    with transaction.atomic():
        try:
            with transaction.atomic():
                ProcessedUpdate.objects.create(update_id=update_id)
        except IntegrityError:
            return DUPLICATE
        return fn(*args, **kwargs)
