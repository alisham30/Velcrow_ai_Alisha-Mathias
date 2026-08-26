"""Command-line tools that read and drive the running system (spec 9).

Nothing here is imported by a service; these are the scripts a human runs.
"""
from __future__ import annotations

import sys
from typing import TextIO


def console(stream: TextIO | None = None) -> TextIO:
    """Return a stream that can actually print a rupee sign.

    Windows terminals default to cp1252, which has no U+20B9, so every rupee
    figure raised UnicodeEncodeError and the Revenue Lab died half way through
    its own table. Chasing the symbol out of the output would have been the
    wrong fix - the numbers are in rupees and should say so.
    """
    out = stream or sys.stdout
    try:
        out.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        pass    # already wrapped, or a stream that cannot be reconfigured
    return out
