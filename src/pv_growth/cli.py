"""Management CLI: python -m pv_growth <command>.

Commands: serve, preflight, smoke, migrate, scheduler, init-flags.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    cmd, *rest = argv

    if cmd == "serve":
        import uvicorn

        from pv_growth.core.config import get_settings
        from pv_growth.main import app

        settings = get_settings()
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
        return 0

    if cmd == "migrate":
        from alembic import command
        from alembic.config import Config

        cfg = Config("alembic.ini")
        cfg.set_main_option("script_location", "alembic")
        command.upgrade(cfg, "head")
        return 0

    if cmd in {"preflight", "smoke"}:
        from pv_growth import preflight, smoke

        module = preflight if cmd == "preflight" else smoke
        return module.run(rest)

    if cmd == "scheduler":
        from pv_growth.jobs.runner import run_forever

        return run_forever()

    if cmd == "init-flags":
        from pv_growth.core.config import get_settings
        from pv_growth.core.flags import FLAG_KEYS
        from pv_growth.database.base import session_scope
        from pv_growth.database.models import FeatureFlag

        settings = get_settings()
        with session_scope(settings) as session:
            from sqlalchemy import select

            existing = {k for (k,) in session.execute(select(FeatureFlag.key)).fetchall()}
            for key in FLAG_KEYS:
                if key not in existing:
                    session.add(FeatureFlag(key=key, enabled=False, note="default off"))
        print(f"flags ready: {len(FLAG_KEYS)}")
        return 0

    print(f"unknown command: {cmd}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
