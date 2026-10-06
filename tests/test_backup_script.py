from __future__ import annotations

import os
import subprocess
from pathlib import Path


def test_backup_script_parses_database_url_with_explicit_port(tmp_path: Path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "pg_dump.args"
    pg_dump = bin_dir / "pg_dump"
    pg_dump.write_text(
        '#!/bin/sh\nprintf \'%s\\n\' "$@" > "$PVG_TEST_ARGS"\nhead -c 4096 /dev/urandom\n',
        encoding="utf-8",
    )
    pg_dump.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "PVG_DATABASE_URL": "postgresql+psycopg://pv_growth:secret@172.23.77.1:5432/pv_growth",
            "PVG_TEST_ARGS": str(args_file),
        }
    )
    backup_dir = tmp_path / "backups"

    result = subprocess.run(  # noqa: S603 - fixed reviewed script + pytest-owned temp path
        ["/bin/bash", "scripts/backup.sh", str(backup_dir)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert args_file.read_text(encoding="utf-8").splitlines() == [
        "-h",
        "172.23.77.1",
        "-p",
        "5432",
        "-U",
        "pv_growth",
        "pv_growth",
    ]
    backups = list(backup_dir.glob("pv_growth-*.sql.gz"))
    assert len(backups) == 1
    assert backups[0].stat().st_size > 0
