"""AWS runner template tests enforce immutable, non-root deployment defaults."""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = REPO_ROOT / "docs" / "deployment" / "aws-runner-cloudformation.yaml"


def test_runner_template_uses_pinned_non_root_release() -> None:
    """Runner bootstrap should not execute a mutable branch or run as root."""
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "YinshiReleaseCommit:" in template_text
    assert "PackageInstallCommand:" not in template_text
    assert 'checkout --detach "${YinshiReleaseCommit}"' in template_text
    assert "PYTHONPATH=/opt/yinshi-runner/source/backend/src" in template_text
    assert "User=yinshi-runner" in template_text
    assert "User=root" not in template_text


def test_runner_template_requires_imdsv2_and_https_only_egress() -> None:
    """Runner metadata and outbound network defaults should be fail closed."""
    template_text = TEMPLATE_PATH.read_text(encoding="utf-8")

    assert "MetadataOptions:" in template_text
    assert "HttpTokens: required" in template_text
    assert "- IpProtocol: tcp" in template_text
    assert "FromPort: 443" in template_text
    assert "ToPort: 443" in template_text
    assert "- IpProtocol: -1" not in template_text
