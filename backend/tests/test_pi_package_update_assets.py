"""Tests for deployment assets that keep the bundled pi package current.

These tests are file-based. The updater runs on the production VM. The timer
must invoke the checked-in script and write Settings release-note status.
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_UNIT = "yinshi-pi-package-update.service"
TIMER_UNIT = "yinshi-pi-package-update.timer"
UPDATE_EXEC = "/opt/yinshi/scripts/update-pi-package.sh --systemd-service"


def test_pi_package_update_script_rebuilds_runtime_assets() -> None:
    """The updater should install latest pi and write status."""
    script_path = REPO_ROOT / "scripts" / "update-pi-package.sh"
    script = script_path.read_text(encoding="utf-8")

    assert "@mariozechner/pi-coding-agent" in script
    assert 'npm view "$package_name" version' in script
    assert 'npm install --prefix "$temporary_dir"' in script
    assert 'run_smoke_test "$temporary_dir"' in script
    assert 'podman build -t "$container_image"' in script
    assert "pi-package-update.json" in script


def test_pi_package_update_timer_runs_daily() -> None:
    """The systemd timer should run daily with persistence for missed runs."""
    service_path = REPO_ROOT / "deploy" / "systemd" / SERVICE_UNIT
    timer_path = REPO_ROOT / "deploy" / "systemd" / TIMER_UNIT
    service = service_path.read_text(encoding="utf-8")
    timer = timer_path.read_text(encoding="utf-8")

    assert f"ExecStart={UPDATE_EXEC}" in service
    assert "OnCalendar=*-*-* 04:17:00 UTC" in timer
    assert "RandomizedDelaySec=1h" in timer
    assert "Persistent=true" in timer
