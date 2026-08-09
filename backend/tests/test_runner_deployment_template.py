"""Runner deployment template checks for bundled services and storage wiring."""

from pathlib import Path

_TEMPLATE_PATH = Path(__file__).parents[2] / "docs/deployment/aws-runner-cloudformation.yaml"


def _template_text() -> str:
    template_text = _TEMPLATE_PATH.read_text(encoding="utf-8")
    assert template_text
    assert "AWS::EC2::Instance" in template_text
    return template_text


def test_runner_template_installs_and_supervises_sidecar() -> None:
    """The deployed runner starts a pinned Node sidecar before the Python agent."""
    template_text = _template_text()

    assert "nodejs22 nodejs22-npm gcc-c++ make" in template_text
    assert 'checkout --detach "${YinshiReleaseCommit}"' in template_text
    assert "npm-22 --prefix /opt/yinshi-runner/source/sidecar" in template_text
    assert (
        "ExecStart=/usr/bin/node-22 /opt/yinshi-runner/source/sidecar/src/index.js" in template_text
    )
    assert "Requires=yinshi-sidecar.service" in template_text
    assert "PYTHONPATH=/opt/yinshi-runner/source/backend/src" in template_text
    assert "SIDECAR_SOCKET_PATH=/var/lib/yinshi/sidecar.sock" in template_text


def test_runner_template_exports_distinct_storage_roots() -> None:
    """Worker databases and shared files receive distinct configured directories."""
    template_text = _template_text()

    assert "YINSHI_RUNNER_SQLITE_DIR=$RUNNER_SQLITE_DIR" in template_text
    assert "YINSHI_RUNNER_SHARED_FILES_DIR=$RUNNER_SHARED_FILES_DIR" in template_text
    assert 'RUNNER_SQLITE_DIR="/var/lib/yinshi/sqlite"' in template_text
    assert 'RUNNER_SHARED_FILES_DIR="/mnt/yinshi-s3-files"' in template_text
    assert 'RUNNER_SHARED_FILES_DIR="/mnt/archil/yinshi"' in template_text
