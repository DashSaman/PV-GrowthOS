"""Feedback: 1–5 ratings; high ratings invite testimonial consent, low
ratings route to recovery (lifecycle). No testimonial publishes without
stored consent."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from pv_growth.core.errors import ValidationError
from pv_growth.core.logging import get_logger
from pv_growth.database.models import FeedbackRating
from pv_growth.events.service import ingest

log = get_logger("feedback")


def submit_rating(
    session: Session, *, user_id: int, rating: int, comment: str | None = None, window_key: str
) -> FeedbackRating:
    """window_key scopes idempotency (e.g. 'svc123:2026-10')."""
    if not 1 <= rating <= 5:
        raise ValidationError("rating must be 1..5")
    dedupe = f"fb:{user_id}:{window_key}"
    existing = session.execute(
        select(FeedbackRating).where(FeedbackRating.dedupe_key == dedupe)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = FeedbackRating(user_id=user_id, rating=rating, comment=comment, dedupe_key=dedupe)
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        return session.execute(select(FeedbackRating).where(FeedbackRating.dedupe_key == dedupe)).scalar_one()

    ingest(
        session,
        "FEEDBACK_RECEIVED",
        user_id=user_id,
        idempotency_key=f"fbev:{dedupe}",
        metadata={"rating": rating, "has_comment": bool(comment)},
    )
    return row


def set_testimonial_consent(session: Session, feedback: FeedbackRating, consent: bool) -> FeedbackRating:
    """Explicit consent recorded — the only gate for publishing a testimonial."""
    if feedback.rating < 4 and consent:
        raise ValidationError("consent only relevant for positive ratings")
    feedback.testimonial_consent = consent
    session.flush()
    return feedback


def route(session: Session, feedback: FeedbackRating) -> str:
    """Routing decision: high → testimonial invite, low → recovery workflow."""
    if feedback.rating >= 4:
        return "testimonial_invite"
    if feedback.rating <= 2:
        return "recovery_workflow"
    return "none"


def publishable_testimonials(session: Session) -> list[FeedbackRating]:
    """Only rows with stored consent may ever be shown publicly."""
    return list(
        session.execute(select(FeedbackRating).where(FeedbackRating.testimonial_consent.is_(True)))
        .scalars()
        .all()
    )
