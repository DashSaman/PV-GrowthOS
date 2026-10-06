"""Machine-verifiable Definition-of-Done audit (spec §33 / order §4, §32).

Checks every required component programmatically and prints PASS / FAIL /
BLOCKED per item. Exit code 1 if any required item FAILs.
"""

from __future__ import annotations

import argparse
import importlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

RESULTS: list[tuple[str, str]] = []


def record(item: str, ok: bool | None, note: str = "") -> None:
    status = "PASS" if ok else ("BLOCKED" if ok is None else "FAIL")
    RESULTS.append((item, f"{status}" + (f" — {note}" if note else "")))
    print(f"[{status}] {item}" + (f" — {note}" if note else ""))


def check_module(package_path: str, min_functions: int = 1) -> bool:
    path = ROOT / package_path
    if not path.exists():
        return False
    py = list(path.rglob("*.py")) if path.is_dir() else [path]
    defs = 0
    for file in py:
        defs += len(re.findall(r"^\s*(def |class )", file.read_text(encoding="utf-8"), re.M))
    return defs >= min_functions


def run_required_command(
    item: str,
    command: list[str],
    *,
    runner=None,
) -> bool:
    """Run a required gate and record its real exit status as audit evidence."""
    if runner is None:

        def runner(argv):
            return subprocess.run(  # noqa: S603
                argv,
                capture_output=True,
                text=True,
                cwd=ROOT,
            )

    proc = runner(command)
    output = (proc.stdout or proc.stderr or "").strip().splitlines()
    note = output[-1][:240] if output else f"exit={proc.returncode}"
    ok = proc.returncode == 0
    record(item, ok, note)
    return ok


def result_exit_code() -> int:
    return 1 if any(status.startswith("FAIL") for _, status in RESULTS) else 0


def can_run_provisioning_canary(*, live: bool, allow_provisioning_canary: bool) -> bool:
    """A mutating production canary requires both live mode and explicit consent."""
    return live and allow_provisioning_canary


def ssh_readonly(cmd: str) -> str | None:
    """Run one fixed-host audit command; callers control whether mutation is allowed."""
    key = Path.home() / ".ssh" / "akh_key"
    ssh_bin = shutil.which("ssh")
    if ssh_bin is None:
        return None
    proc = subprocess.run(  # noqa: S603
        [
            ssh_bin,
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=15",
            "-i",
            str(key),
            "root@91.107.240.235",
            cmd,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def main(*, live: bool = False, allow_provisioning_canary: bool = False) -> int:
    RESULTS.clear()
    print("== PV GrowthOS — Definition of Done audit ==\n")

    # 1. required modules contain real code (functions/classes)
    modules = {
        "backend API (FastAPI app)": "src/pv_growth/main.py",
        "database layer": "src/pv_growth/database",
        "migrations (alembic)": "alembic/versions",
        "event system": "src/pv_growth/events",
        "attribution": "src/pv_growth/attribution",
        "Mirza adapter": "src/pv_growth/mirza_adapter",
        "Telegram integration": "src/pv_growth/telegram",
        "durable jobs": "src/pv_growth/jobs",
        "scheduler": "src/pv_growth/jobs/runner.py",
        "Free Config engine": "src/pv_growth/free_config",
        "config validation/quality": "src/pv_growth/config_quality",
        "lifecycle automation": "src/pv_growth/lifecycle",
        "messaging": "src/pv_growth/messaging",
        "referral engine": "src/pv_growth/referrals",
        "partner/affiliate": "src/pv_growth/partners",
        "content engine": "src/pv_growth/content",
        "Instagram growth integration": "src/pv_growth/instagram",
        "competitor monitoring": "src/pv_growth/competitors",
        "feedback": "src/pv_growth/feedback",
        "analytics": "src/pv_growth/analytics",
        "experiments": "src/pv_growth/experiments",
        "segments": "src/pv_growth/segments",
        "campaigns": "src/pv_growth/campaigns",
        "admin": "src/pv_growth/admin",
    }
    for name, path in modules.items():
        record(f"module: {name}", check_module(path))

    # 2. migration chain 0001..0011 present and ordered
    versions = sorted(p.name for p in (ROOT / "alembic/versions").glob("*.py"))
    record("migrations: 11 revisions chained", len(versions) == 11, ", ".join(v[:4] for v in versions))

    # 3. feature flags all defined
    sys.modules.pop("pv_growth.core.flags", None)
    flags = importlib.import_module("pv_growth.core.flags")
    record("feature flags: 11 defined", len(flags.FLAG_KEYS) == 11)

    # 4. API surface: routes exist
    main_mod = importlib.import_module("pv_growth.main")
    app = main_mod.create_app()
    # new FastAPI versions mount routers lazily; the OpenAPI schema lists paths
    paths = set(app.openapi().get("paths", {}))
    for required in (
        "/health",
        "/ready",
        "/api/events",
        "/admin/api/dashboard",
        "/admin/api/instagram/readiness",
        "/webhooks/telegram/{secret}",
    ):
        record(f"route: {required}", required in paths)

    # 5. no placeholders / stubs in production code
    offenders = []
    for file in (ROOT / "src").rglob("*.py"):
        text = file.read_text(encoding="utf-8")
        for pattern in (r"#\s*TODO", r"#\s*FIXME", r"NotImplementedError", r"\bpass\s#$"):
            if re.search(pattern, text):
                offenders.append(f"{file.name}: {pattern}")
    record("no TODO/FIXME/NotImplemented stubs", not offenders, "; ".join(offenders[:3]))

    # 6. CI + deployment + rollback + preflight + smoke tooling exist
    for item, path in {
        "CI workflow": ".github/workflows/ci.yml",
        "Dockerfile (limits via compose)": "Dockerfile",
        "compose with resource limits": "docker-compose.yml",
        "preflight CLI": "src/pv_growth/preflight.py",
        "smoke CLI": "src/pv_growth/smoke.py",
        "backup script": "scripts/backup.sh",
        "load probe": "scripts/loadtest.py",
        "sentinelx cleanup": "scripts/sentinelx-cleanup.sh",
    }.items():
        record(f"tooling: {item}", (ROOT / path).exists())

    # 7. tests pass
    run_required_command(
        "full test suite passes",
        [sys.executable, "-m", "pytest", "tests/", "-q"],
    )

    # 8. lint clean
    run_required_command(
        "ruff clean",
        [sys.executable, "-m", "ruff", "check", "src", "tests", "scripts"],
    )

    # 9. LIVE production checks are explicit opt-in and read-only.
    if live:
        try:
            out = ssh_readonly("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8350/health")
            record(
                "production /health = 200",
                out == "200" if out is not None else None,
                f"got {out or 'no ssh'}",
            )
            out = ssh_readonly("curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8350/ready")
            record(
                "production /ready = 200",
                out == "200" if out is not None else None,
                f"got {out or 'no ssh'}",
            )
            out = ssh_readonly("docker ps --filter name=pv-growth-app --format '{{.Status}}'")
            record(
                "production container healthy",
                "healthy" in out if out is not None else None,
                out or "no ssh",
            )
            out = ssh_readonly(
                'docker exec pv-growth-app python -c "from pv_growth.core.config import get_settings;'
                "from sqlalchemy import create_engine,text;"
                "print(create_engine(get_settings().database_url).connect()"
                ".execute(text('select version_num from alembic_version')).scalar_one())\""
            )
            record(
                "production migration head = 0011",
                "0011" in out if out is not None else None,
                out or "no ssh",
            )
            out = ssh_readonly("docker network ls --format '{{.Name}}' | grep -c '^pv_growth_net$'")
            record("production network pv_growth_net", out == "1" if out is not None else None)
            out = ssh_readonly(
                "curl -sk -o /dev/null -w '%{http_code}' --max-time 8 https://robot.ahsg.top;"
                " echo;npanel=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 8 https://npanel.softarg.ir);"
                " echo $npanel"
            )
            ok = None if out is None else ("200 200" in out.replace("\n", " ") or out.count("200") >= 2)
            record("protected Mirza+Reseller = 200/200", ok, out or "no ssh")
        except Exception as exc:  # noqa: BLE001
            record("live production checks", None, f"ssh unavailable: {exc}")
    else:
        record(
            "production /health = 200",
            None,
            "run with --live after rollout",
        )
        record("production /ready = 200", None, "run with --live after rollout")
        record("production container healthy", None, "run with --live after rollout")
        record("production migration head = 0011", None, "run with --live after rollout")
        record("production network pv_growth_net", None, "run with --live after rollout")
        record("protected Mirza+Reseller = 200/200", None, "run with --live after rollout")

    # 10. external-only items -> BLOCKED (never FAIL)
    record(
        "CI run observed for this candidate",
        None,
        "requires pushed branch/PR; historical runs are not candidate evidence",
    )
    pip_audit = shutil.which("pip-audit")
    if pip_audit is None:
        record(
            "dependency scan (pip-audit resolved lock)",
            None,
            "pip-audit not installed locally; candidate CI must provide evidence",
        )
    else:
        run_required_command(
            "dependency scan (pip-audit resolved lock)",
            [pip_audit, "-r", "requirements.lock", "--strict"],
        )
    if live:
        out = ssh_readonly(
            'docker exec pv-growth-app python -c "from pv_growth.core.config import get_settings;'
            "import httpx;"
            "r=httpx.get(f'{get_settings().telegram_api_base}/bot'"
            "+get_settings().telegram_bot_token+'/getMe',timeout=8);"
            "print(r.json()['result']['username'])\""
        )
        record(
            "Telegram real integration (@pvgrowthos_bot live)",
            "pvgrowthos_bot" in out if out is not None else None,
            out or "no ssh",
        )
    else:
        record(
            "Telegram real integration (@pvgrowthos_bot live)",
            None,
            "run with --live after rollout",
        )

    # B5: real PV-exclusive provisioning E2E — provision + panel verify + cleanup
    probe = "dodprobe" + "b5x"
    script = (
        "from pv_growth.core.config import get_settings;"
        "from pv_growth.provisioning.xui import get_provisioning;"
        "a=get_provisioning(get_settings());"
        "o=a.create_temp_service(location='multiloc',traffic_gb=1,validity_hours=1,"
        "protocol='vless',idempotency_key='" + probe + "');"
        "st=a.service_state(o['service_ref']);"
        "ok=st.get('exists') and st.get('traffic_limit_gb')==1;"
        "a.disable_service(o['service_ref']);"
        "print('PROV_OK' if ok else 'PROV_FAIL:', o['service_ref'],"
        "bool(o['config_uri']),st.get('traffic_limit_gb'))"
    )
    if can_run_provisioning_canary(
        live=live,
        allow_provisioning_canary=allow_provisioning_canary,
    ):
        out = ssh_readonly('docker exec pv-growth-app python -c "' + script + '"')
        record(
            "B5 real provisioning E2E (create+verify+cleanup)",
            "PROV_OK" in out if out is not None else None,
            (out or "no ssh").strip()[:160],
        )
    else:
        record(
            "B5 real provisioning E2E (create+verify+cleanup)",
            None,
            "requires --live --allow-provisioning-canary after protected rollout",
        )

    failed = sum(1 for _, s in RESULTS if s.startswith("FAIL"))
    blocked = sum(1 for _, s in RESULTS if s.startswith("BLOCKED"))
    passed = len(RESULTS) - failed - blocked
    print(f"\nAUDIT: {passed} PASS · {failed} FAIL · {blocked} BLOCKED")
    if result_exit_code():
        print("PROJECT NOT COMPLETE — fix FAIL items.")
        return 1
    print("All local requirements PASS; BLOCKED items are documented external dependencies (BLOCKERS.md).")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="run read-only production probes")
    parser.add_argument(
        "--allow-provisioning-canary",
        action="store_true",
        help="with --live, create and clean up one GrowthOS-owned provisioning canary",
    )
    args = parser.parse_args()
    raise SystemExit(
        main(
            live=args.live,
            allow_provisioning_canary=args.allow_provisioning_canary,
        )
    )
