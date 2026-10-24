"""Cache only fully inspected stable non-human threads, without message bodies."""

import hashlib
import json
import re

from sqlalchemy import text

from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig

POLICY_VERSION = 20261025  # Bump when thread-inspection policy changes.
STABLE = {"held_no_new_human_reply", "ignore_automatic", "ignore_spam"}


def identity(campaign, key, route, thread):
    tid, history = thread.get("id", ""), thread.get("historyId", "")
    if not isinstance(tid, str) or not re.fullmatch(r"[a-fA-F0-9]{1,128}", tid):
        return None
    if not isinstance(history, str) or not re.fullmatch(r"[0-9]{1,32}", history):
        return None
    namespace = json.dumps([POLICY_VERSION, campaign, key, route["email"].lower(), route["subject"]])
    return (
        "mail:read:" + hashlib.sha224(namespace.encode()).hexdigest(),
        hashlib.sha256(json.dumps([tid, history]).encode()).hexdigest(),
    )


def read(settings, campaign, key, route, thread):
    token = identity(campaign, key, route, thread)
    if token is None:
        return None
    with session_scope(settings) as db:
        if db.bind.dialect.name == "postgresql":
            db.execute(text("SET TRANSACTION READ ONLY"))
        if settings.env == "production" and (
            db.bind.dialect.name != "postgresql"
            or db.scalar(text("SELECT current_database()")) != "pv_growth"
        ):
            raise ValueError("unexpected mail cache database")
        row = db.get(AppConfig, token[0])
        if row and row.value.get("version") == token[1] and row.value.get("state") in STABLE:
            return row.value["state"]
    return None


def save(settings, campaign, key, route, thread, state):
    token = identity(campaign, key, route, thread)
    if token is None or state not in STABLE:
        return
    with session_scope(settings) as db:
        if settings.env == "production" and (
            db.bind.dialect.name != "postgresql"
            or db.scalar(text("SELECT current_database()")) != "pv_growth"
        ):
            raise ValueError("unexpected mail cache database")
        row = db.get(AppConfig, token[0], with_for_update=True)
        value = {"version": token[1], "state": state}
        if row:
            row.value = value
        else:
            db.add(AppConfig(key=token[0], value=value))
