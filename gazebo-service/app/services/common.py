from functools import wraps
from typing import Any
from google.protobuf import json_format


def proto_dict(msg: Any) -> dict[str, Any]:
    return json_format.MessageToDict(msg, preserving_proto_field_name=True)


def serialized(fn):
    """Serialize service operations against the runtime's shared lock."""
    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return fn(self, *args, **kwargs)
    return wrapper


def _settings_match(actual, expected):
    import math
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(_settings_match(actual.get(key), value) for key, value in expected.items())
    if isinstance(expected, (list, tuple)):
        return isinstance(actual, (list, tuple)) and len(actual) == len(expected) and all(_settings_match(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, (int, float)):
        return actual is not None and math.isclose(actual, expected, rel_tol=1e-7, abs_tol=1e-7)
    return actual == expected
