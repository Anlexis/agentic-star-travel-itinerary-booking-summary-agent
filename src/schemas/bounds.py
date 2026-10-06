"""AgentCore Platform v1.0"""

# Shared reader for the runtime bounds declared in config/config.yaml.
#
# The bounds are seeded into state as a JSON string (structured state fields are
# stored serialized for checkpoint safety). Every node that enforces a declared
# bound reads it through this module, so there is one parse and one fallback
# rather than a copy per node that could drift from the shipped file.

import json
from typing import Any, Dict, Mapping, Optional

# Mirrors the itinerary block of config/config.yaml. Used only when a graph was
# built outside an invocation and no bounds were seeded — never to override a
# declared value.
_FALLBACK: Dict[str, Any] = {
    "max_bookings": 500,
    "max_field_chars": 400,
    "max_report_chars": 120000,
    "min_cost_jpy": 0,
    "max_cost_jpy": 100000000,
}


def read_bounds(state: Mapping[str, Any]) -> Dict[str, Any]:
    """Return the itinerary bounds seeded into ``state``.

    Reads ``runtime_limits`` from the state the caller is actually running in.
    A node that reached for a key belonging to a different graph layer would
    compare against an empty mapping on every real invocation, so the key name
    and the layer are deliberately the same in both graphs.
    """
    raw = state.get("runtime_limits")
    merged = dict(_FALLBACK)
    if isinstance(raw, str) and raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            return merged
        if isinstance(parsed, dict):
            merged.update(parsed)
    elif isinstance(raw, dict):
        merged.update(raw)
    return merged


def bound_int(bounds: Mapping[str, Any], key: str) -> int:
    """Return a bound as an int, falling back to the shipped value.

    A bound that arrives non-numeric — an operator typo in config/config.yaml —
    must not disable the limit it configures, so the shipped value stands in.
    """
    value = bounds.get(key, _FALLBACK[key])
    checked = finite_in_range(value, None, None)
    if checked is None:
        return int(_FALLBACK[key])
    return int(checked)


def finite_in_range(
    value: Any,
    minimum: Optional[float],
    maximum: Optional[float],
) -> Optional[float]:
    """Return ``value`` as a finite float inside the bounds, else ``None``.

    Rejects booleans (``True`` is not the number 1 in a booking record),
    non-numeric text, and the non-finite floats. NaN and the infinities parse
    fine through ``float()`` and arrive intact through raw JSON, and every
    comparison against NaN is False — so a range check written the obvious way
    accepts them silently and the value flows on into the arithmetic. Returning
    ``None`` here forces the caller to fail closed.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        candidate = float(value)
    elif isinstance(value, str):
        try:
            candidate = float(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    if candidate != candidate or candidate in (float("inf"), float("-inf")):
        return None
    if minimum is not None and candidate < minimum:
        return None
    if maximum is not None and candidate > maximum:
        return None
    return candidate
