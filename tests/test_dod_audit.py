"""Definition-of-Done audit must fail on failed required evidence."""

from __future__ import annotations

import importlib.util
import inspect
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _audit_module():
    spec = importlib.util.spec_from_file_location("dod_audit", ROOT / "scripts/dod_audit.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_required_command_failure_is_a_real_audit_failure():
    audit = _audit_module()
    audit.RESULTS.clear()

    def _failed(command):
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="vulnerability")

    ok = audit.run_required_command(
        "dependency scan",
        ["pip-audit", "-r", "requirements.lock", "--strict"],
        runner=_failed,
    )

    assert ok is False
    assert audit.result_exit_code() == 1
    assert any(item == "dependency scan" and status.startswith("FAIL") for item, status in audit.RESULTS)


def test_ci_required_gates_are_not_soft_failed_and_main_exports_sha_image():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert "ruff format --check src tests || true" not in workflow
    assert "pip-audit -r requirements" in workflow
    assert "pip-audit -r requirements.lock --strict || true" not in workflow
    assert "docker save" in workflow
    assert "sha256sum" in workflow
    assert "actions/upload-artifact@v4" in workflow
    assert "pv-growth-app:${{ github.sha }}" in workflow


def test_live_probes_and_provisioning_canary_are_explicit_opt_in():
    audit = _audit_module()
    signature = inspect.signature(audit.main)

    assert signature.parameters["live"].default is False
    assert signature.parameters["allow_provisioning_canary"].default is False
    assert audit.can_run_provisioning_canary(live=False, allow_provisioning_canary=True) is False
    assert audit.can_run_provisioning_canary(live=True, allow_provisioning_canary=False) is False
    assert audit.can_run_provisioning_canary(live=True, allow_provisioning_canary=True) is True
