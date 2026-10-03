"""Domain exceptions shared across modules."""

from __future__ import annotations


class GrowthError(Exception):
    """Base class for all GrowthOS domain errors."""


class NotConfigured(GrowthError):
    """An external integration (token/URL) is not configured yet."""


class ExternalServiceError(GrowthError):
    """A remote dependency failed; never crash the app because of it."""


class ValidationError(GrowthError):
    """Invalid input or an invalid domain state transition."""


class Forbidden(GrowthError):
    """Authentication/authorization failure."""
