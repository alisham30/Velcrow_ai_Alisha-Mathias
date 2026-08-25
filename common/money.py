"""One money formatter for the whole system (spec 3, 11).

All money is integer paise. Anything a human reads is rendered here, in
rupees with Indian digit grouping, so the backend, the storefront and the
agent can never disagree about what an amount says.
"""
from __future__ import annotations

RUPEE = "₹"


def rupees(paise: int) -> str:
    """1438200 -> '₹14,382.00'. Integer paise only; never floats."""
    if not isinstance(paise, int):
        raise TypeError(f"money must be integer paise, got {type(paise).__name__}")
    sign = "-" if paise < 0 else ""
    whole, frac = divmod(abs(paise), 100)
    digits = str(whole)
    if len(digits) > 3:  # last three, then groups of two
        head, tail = digits[:-3], digits[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        digits = ",".join(parts + [tail])
    return f"{sign}{RUPEE}{digits}.{frac:02d}"
