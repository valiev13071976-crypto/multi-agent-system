"""Best-effort emit helper — never raises into business paths."""


def safe_emit(observability, event_type: str, *, context=None, **kwargs):
    if observability is None:
        return None
    try:
        return observability.emit(event_type, context=context, **kwargs)
    except Exception:
        return None
