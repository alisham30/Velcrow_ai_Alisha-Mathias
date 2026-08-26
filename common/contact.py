"""One normalisation for the shopper key (spec 7.3, 7.2).

The shopper key is a contact the person already gives a shop - a phone number
or an email - not an account and not a credential. It exists so a basket
bought last week is findable next week, on a different device, with nothing
but the thing they would have typed anyway.

Normalisation matters because the same person types the same contact
differently every time: "+91 98211 58848", "098211 58848" and "9821158848"
are one shopper, and "Zoya@Example.com " is the same as "zoya@example.com".
Without this, every spelling is a different person and the history is lost
exactly as if it had never been stored.

The raw text the shopper typed is kept separately for display; only the
normalised key is ever matched on.

NOT a credential. Nothing here proves the person owns the contact, so the key
must never be treated as authentication - see identify() callers.
"""
from __future__ import annotations

import re

MIN_PHONE_DIGITS = 10


class InvalidContact(ValueError):
    """The contact is not recognisably a phone number or an email."""


def looks_like_email(raw: str) -> bool:
    return "@" in raw


def normalise(raw: str) -> str:
    """Return the match key for a typed contact, or raise InvalidContact.

    Emails lower-case and trim. Phone numbers reduce to their last 10 digits,
    which collapses +91 / 0 / spacing variants onto one key.
    """
    text = (raw or "").strip()
    if not text:
        raise InvalidContact("a phone number or email is required")

    if looks_like_email(text):
        email = text.lower()
        local, _, domain = email.partition("@")
        if not local or "." not in domain or domain.startswith(".") or domain.endswith("."):
            raise InvalidContact(f"'{raw}' is not a valid email address")
        return f"email:{email}"

    digits = re.sub(r"\D", "", text)
    if len(digits) < MIN_PHONE_DIGITS:
        raise InvalidContact(
            f"'{raw}' is not a valid phone number or email "
            f"(a phone needs at least {MIN_PHONE_DIGITS} digits)"
        )
    return f"phone:{digits[-MIN_PHONE_DIGITS:]}"


def display(raw: str) -> str:
    """What to show the shopper back - their own text, tidied, never the key."""
    return (raw or "").strip()
