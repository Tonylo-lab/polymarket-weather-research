"""Pure numerical validation only; no SDK, accounts, RPC or credential access."""
from decimal import Decimal


class ProbeError(Exception):
    """Static error code; never includes remote responses or secret input."""


def require(condition, code):
    if not condition:
        raise ProbeError(code)


def decimal(value):
    require(not isinstance(value, bool), "INVALID_NUMBER")
    try:
        result = Decimal(str(value))
    except Exception:
        raise ProbeError("INVALID_NUMBER") from None
    require(result.is_finite(), "INVALID_NUMBER")
    return result
