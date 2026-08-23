import time
import uuid

LARGE_AMOUNT_THRESHOLD = 10_000
CONFIRMATION_TTL_SECONDS = 60

# token -> {"admin_id": int, "created_at": float, "fn": callable, "args": tuple, "summary": str}
_PENDING = {}


def create_pending(admin_id, fn, args, summary):
    token = uuid.uuid4().hex
    _PENDING[token] = {
        "admin_id": admin_id,
        "created_at": time.monotonic(),
        "fn": fn,
        "args": args,
        "summary": summary,
    }
    return token


def peek_pending(token):
    entry = _PENDING.get(token)
    if entry is None:
        return None
    if time.monotonic() - entry["created_at"] > CONFIRMATION_TTL_SECONDS:
        _PENDING.pop(token, None)
        return None
    return entry


def pop_pending(token):
    entry = peek_pending(token)
    if entry is None:
        return None
    _PENDING.pop(token, None)
    return entry
