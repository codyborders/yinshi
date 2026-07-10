"""Race-focused workspace file tests using disposable filesystem fixtures."""

from pathlib import Path

import pytest


def test_workspace_file_write_rejects_symlinked_parent(tmp_path: Path) -> None:
    """A browser edit must not follow a parent symlink outside its workspace."""
    from yinshi.services.workspace_files import write_text_file

    workspace_path = tmp_path / "workspace"
    outside_directory = tmp_path / "outside"
    workspace_path.mkdir()
    outside_directory.mkdir()
    outside_file = outside_directory / "target.txt"
    outside_file.write_text("outside", encoding="utf-8")
    (workspace_path / "switch").symlink_to(outside_directory, target_is_directory=True)

    with pytest.raises(PermissionError, match="symlink"):
        write_text_file(str(workspace_path), "switch/target.txt", "changed")

    assert outside_file.read_text(encoding="utf-8") == "outside"
