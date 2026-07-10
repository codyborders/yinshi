"""Deployment assets must not auto-execute unreviewed npm releases."""

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
    """Sidecar builds should use Pi's minimum Node release through a pinned digest."""
    dockerfile = (REPO_ROOT / "sidecar" / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.startswith("FROM node:22.19.0-slim@sha256:")
    assert "FROM node:20" not in dockerfile
