"""Datetime helpers.

Provides UTC timestamp helpers without using deprecated ``datetime.utcnow()``.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime.

    The project currently stores UTC timestamps as naive datetimes in models.
    This helper avoids ``datetime.utcnow()`` deprecation while preserving that
    storage/serialization behavior.
    """
    return datetime.now(UTC).replace(tzinfo=None)

