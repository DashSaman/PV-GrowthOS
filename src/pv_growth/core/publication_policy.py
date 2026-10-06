"""Explicit owner prohibition, independent of campaign and feature flags."""

from pv_growth.core.errors import ValidationError

FORBIDDEN_MAIN = {"-1003855234264", "@pvnetwork0", "pvnetwork0"}


def refuse_main_channel(destination: int | str) -> None:
    if str(destination).strip().lower() in FORBIDDEN_MAIN:
        raise ValidationError("publication in the main channel is forbidden by the owner")
