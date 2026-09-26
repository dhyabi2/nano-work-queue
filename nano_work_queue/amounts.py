"""Exact XNO amounts.

1 XNO = 10**30 raw. A float cannot hold 30 significant digits, so every
amount in this service is carried as an integer count of raw and is only
ever rendered to a decimal *string*. No float touches the money path.
"""

from decimal import Decimal, InvalidOperation

RAW_PER_XNO = 10**30

MIN_PRICE_RAW = 10**24          # "0.000001" XNO
MAX_PRICE_RAW = 100 * RAW_PER_XNO  # "100" XNO
MAX_DP = 30                     # a raw is the smallest unit; 30 dp is exact
MIN_RENDER_DP = 6               # the wire format never shows fewer than 6 dp


class AmountError(ValueError):
    """An amount that is not a usable decimal string."""


def parse_xno(text) -> int:
    """A decimal XNO *string* -> integer raw. Rejects anything else.

    Floats and ints are refused outright: accepting 0.1 as a float is how
    a rail starts losing digits, so the caller must send a string.
    """
    if isinstance(text, bool) or not isinstance(text, str):
        raise AmountError("amount must be a decimal string, not a number")
    s = text.strip()
    if not s or s[0] in "+-":
        raise AmountError(f"amount must be a positive decimal string, got {text!r}")
    # Decimal accepts "1e-3", "Inf" and "NaN"; none of those is a decimal
    # string, and each would smuggle a non-exact amount into the money path.
    if s.count(".") > 1 or not all(ch.isdigit() or ch == "." for ch in s):
        raise AmountError(f"amount is not a plain decimal string: {text!r}")
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise AmountError(f"amount is not a decimal: {text!r}") from None
    exponent = d.as_tuple().exponent
    if exponent < -MAX_DP:
        raise AmountError(f"amount has more than {MAX_DP} decimal places: {text!r}")
    return int(d.scaleb(30))


def format_xno(raw: int) -> str:
    """Integer raw -> the canonical decimal string.

    Exact: the digits come from integer division, never from a float. At
    least 6 dp, and more only when the amount genuinely needs them, with
    no trailing-zero noise beyond that.
    """
    if not isinstance(raw, int) or isinstance(raw, bool):
        raise AmountError("raw amount must be an int")
    if raw < 0:
        raise AmountError("raw amount must not be negative")
    whole, frac = divmod(raw, RAW_PER_XNO)
    digits = f"{frac:030d}"
    trimmed = digits.rstrip("0")
    dp = max(MIN_RENDER_DP, len(trimmed))
    return f"{whole}.{digits[:dp]}"


def in_price_range(raw: int) -> bool:
    return MIN_PRICE_RAW <= raw <= MAX_PRICE_RAW


def add(*raws: int) -> int:
    """Sum raw amounts. Integer addition, so 0.1 + 0.2 is exactly 0.3."""
    total = 0
    for r in raws:
        if not isinstance(r, int) or isinstance(r, bool):
            raise AmountError("raw amount must be an int")
        total += r
    return total
