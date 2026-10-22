"""Deployment assets must not auto-execute unreviewed npm releases."""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_mutable_production_pi_updater_is_absent() -> None:
    """Production should advance Pi only through reviewed lockfile deployments."""
    unsafe_paths = [
        REPO_ROOT / "scripts" / "update-pi-package.sh",
        REPO_ROOT / "deploy" / "systemd" / "yinshi-pi-package-update.service",
        REPO_ROOT / "deploy" / "systemd" / "yinshi-pi-package-update.timer",
    ]

    assert [path for path in unsafe_paths if path.exists()] == []


def test_sidecar_image_uses_supported_immutable_node_base() -> None:
    """Sidecar builds must retain the certificate fix and immutable image pin."""
    dockerfile = (REPO_ROOT / "sidecar" / "Dockerfile").read_text(encoding="utf-8")

    assert re.match(r"FROM node:22\.23\.4-slim@sha256:[0-9a-f]{64}\n", dockerfile)
    assert "ge 20250419~deb12u2" in dockerfile
    assert "FROM node:20" not in dockerfile
