from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from pv_growth.database.types import Base, Json, utcnow


class Experiment(Base):
    """Deterministic A/B experiments. Assignment is hash(user, key, seed) —
    stable for the life of the experiment (never re-assigned while running)."""

    __tablename__ = "experiments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    variants: Mapped[list] = mapped_column(Json, nullable=False)  # ["A","B"]
    split_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    seed: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    metric_event: Mapped[str] = mapped_column(String(32), nullable=False, default="PAYMENT_SUCCESS")
    created_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime())


class ExperimentAssignment(Base):
    """Persisted assignment — unique per (experiment, user); while an
    experiment runs this row NEVER changes."""

    __tablename__ = "experiment_assignments"
    __table_args__ = (UniqueConstraint("experiment_id", "user_id", name="uq_exp_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("experiments.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    variant: Mapped[str] = mapped_column(String(32), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow)


class AppConfig(Base):
    """Runtime-configurable values (lead-scoring weights, thresholds…).
    Business values live in data, not code."""

    __tablename__ = "app_configs"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(Json, nullable=False, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False, default=utcnow, onupdate=utcnow)
