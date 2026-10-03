"""Deterministic A/B experiments.

assignment = hash(experiment.seed, experiment.key, user_id) → stable forever.
Persisted per (experiment, user); never changed while the experiment runs.
Results are reported, not auto-declared (weak samples declare nothing)."""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.database.models import Event, Experiment, ExperimentAssignment

MIN_SAMPLE_PER_VARIANT = 30  # below this, results are "insufficient_sample"


def create_experiment(session: Session, *, key: str, name: str, variants: list[str],
                      split_percent: int = 50, metric_event: str = "PAYMENT_SUCCESS"
                      ) -> Experiment:
    if len(variants) < 2:
        raise ValidationError("an experiment needs at least two variants")
    if not 1 <= split_percent <= 99:
        raise ValidationError("split_percent must be 1..99")
    existing = session.execute(
        select(Experiment).where(Experiment.key == key)
    ).scalar_one_or_none()
    if existing is not None:
        raise ValidationError(f"experiment '{key}' already exists")
    exp = Experiment(key=key, name=name, variants=variants,
                     split_percent=split_percent, seed=secrets.token_hex(8),
                     metric_event=metric_event)
    session.add(exp)
    session.flush()
    return exp


def deterministic_variant(exp: Experiment, user_id: int) -> str:
    """Pure function of (seed, key, user) — no randomness at assignment time."""
    basis = f"{exp.seed}:{exp.key}:{user_id}"
    digest = hashlib.sha256(basis.encode()).hexdigest()
    bucket = int(digest[:8], 16) % 100
    return exp.variants[0] if bucket < exp.split_percent else exp.variants[1]


def assign(session: Session, user_id: int, experiment_key: str) -> str:
    """Idempotent: first call persists; later calls return the SAME variant,
    even if split_percent changes mid-run."""
    exp = session.execute(
        select(Experiment).where(Experiment.key == experiment_key)
    ).scalar_one_or_none()
    if exp is None:
        raise ValidationError(f"unknown experiment: {experiment_key}")
    if exp.status != "running":
        raise ValidationError("experiment not running")

    existing = session.execute(
        select(ExperimentAssignment).where(
            ExperimentAssignment.experiment_id == exp.id,
            ExperimentAssignment.user_id == user_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing.variant  # never re-assign while running

    variant = deterministic_variant(exp, user_id)
    assignment = ExperimentAssignment(experiment_id=exp.id, user_id=user_id,
                                      variant=variant)
    session.add(assignment)
    session.flush()
    return variant


def results(session: Session, experiment_key: str) -> dict:
    """Per-variant: users, conversions, rate. No winner declared on weak samples."""
    exp = session.execute(
        select(Experiment).where(Experiment.key == experiment_key)
    ).scalar_one_or_none()
    if exp is None:
        raise ValidationError(f"unknown experiment: {experiment_key}")

    assignments = session.execute(
        select(ExperimentAssignment).where(
            ExperimentAssignment.experiment_id == exp.id)
    ).scalars().all()
    per_variant: dict[str, dict] = {
        v: {"users": 0, "conversions": 0} for v in exp.variants
    }
    for a in assignments:
        per_variant[a.variant]["users"] += 1
        hit = session.execute(
            select(func.count()).select_from(Event).where(
                Event.user_id == a.user_id, Event.event_type == exp.metric_event)
        ).scalar_one()
        per_variant[a.variant]["conversions"] += 1 if hit else 0

    report = {
        "key": exp.key, "metric": exp.metric_event, "status": exp.status,
        "variants": {},
        "sufficient_sample": True, "declared_winner": None,
    }
    for variant, stats in per_variant.items():
        users = stats["users"]
        report["variants"][variant] = {
            **stats,
            "rate": round(stats["conversions"] / users, 4) if users else 0.0,
        }
        if users < MIN_SAMPLE_PER_VARIANT:
            report["sufficient_sample"] = False
    return report


def end_experiment(session: Session, experiment_key: str) -> Experiment:
    exp = session.execute(
        select(Experiment).where(Experiment.key == experiment_key)
    ).scalar_one_or_none()
    if exp is None:
        raise ValidationError(f"unknown experiment: {experiment_key}")
    from pv_growth.database.types import utcnow
    exp.status = "ended"
    exp.ended_at = utcnow()
    return exp
