"""Import only a fresh public local catalogue; no customer/payment handlers."""

import json
import sys

from sqlalchemy import text

from pv_growth.acquisition.retail_catalogue import KEY, validate_catalogue
from pv_growth.core.config import get_settings
from pv_growth.database.base import session_scope
from pv_growth.database.models import AppConfig


def store(session, value):
    clean = validate_catalogue(value)
    if clean is None:
        raise ValueError("invalid public catalogue")
    clean.pop("recommended", None)
    row = session.get(AppConfig, KEY)
    if row is None:
        session.add(AppConfig(key=KEY, value=clean))
    else:
        row.value = clean
    session.commit()
    return len(clean["plans"])


def main():
    raw = sys.stdin.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("catalogue size bound")
    settings = get_settings()
    with session_scope(settings) as session:
        if session.bind.dialect.name != "postgresql":
            raise ValueError("unexpected database dialect")
        if session.scalar(text("SELECT current_database()")) != "pv_growth":
            raise ValueError("unexpected database")
        count = store(session, json.loads(raw))
    print(json.dumps({"status": "stored", "public_plans": count}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("catalogue_import: unavailable", file=sys.stderr)
        sys.exit(1)
