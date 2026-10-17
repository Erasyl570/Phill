"""Runtime safeguards; deliberately independent of bot prompts."""
import hashlib
import os
import tempfile


def acquire_polling_lock(token):
    """Hold the returned file open for the lifetime of this process (Linux)."""
    import fcntl

    name = hashlib.sha256(token.encode()).hexdigest()[:24]
    handle = open(os.path.join(tempfile.gettempdir(), f"phill-{name}.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        raise RuntimeError("Другой процесс Фила уже использует этот Telegram-токен") from None
    return handle


def reply_models(primary, vision, available, preferred, blocked, image=False):
    candidates = ([vision] if image else []) + [primary] + list(preferred) + list(available)
    return list(dict.fromkeys(
        m for m in candidates
        if m and m in available and m not in blocked
    ))[:3]
