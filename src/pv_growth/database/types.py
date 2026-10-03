"""Shared declarative base and portable column types."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)  # naive UTC everywhere


class Base(DeclarativeBase):
    pass


# JSON that becomes JSONB on PostgreSQL and plain JSON on SQLite (tests).
Json = JSON().with_variant(JSONB(), "postgresql")

UtcDateTime = DateTime()
