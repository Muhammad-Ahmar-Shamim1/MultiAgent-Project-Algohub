"""resolve_workspace_path: path-traversal protection (FR-064)."""

from pathlib import Path

import pytest

from web_department.workstation.tools import PathEscapeError, resolve_workspace_path


def test_relative_path_inside_the_workspace_resolves(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()

    resolved = resolve_workspace_path(str(tmp_path), "src/app.py")

    assert resolved == (tmp_path / "src" / "app.py").resolve()


def test_dot_and_bare_relative_paths_resolve_to_the_workspace_root(tmp_path: Path) -> None:
    assert resolve_workspace_path(str(tmp_path), ".") == tmp_path.resolve()
    assert resolve_workspace_path(str(tmp_path), "") == tmp_path.resolve()


@pytest.mark.parametrize(
    "escape",
    [
        "../secrets.txt",
        "../../etc/passwd",
        "a/../../b",
        "./../../x",
        "src/../../../x",
    ],
)
def test_parent_directory_traversal_is_blocked(tmp_path: Path, escape: str) -> None:
    with pytest.raises(PathEscapeError):
        resolve_workspace_path(str(tmp_path), escape)


def test_absolute_path_outside_the_workspace_is_blocked(tmp_path: Path) -> None:
    with pytest.raises(PathEscapeError):
        resolve_workspace_path(str(tmp_path), "/etc/passwd")


def test_absolute_path_that_happens_to_be_inside_the_workspace_resolves(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"

    resolved = resolve_workspace_path(str(tmp_path), str(target))

    assert resolved == target.resolve()


def test_a_sneaky_prefix_match_is_not_enough(tmp_path: Path) -> None:
    """`/workspace-evil` must not be treated as inside `/workspace`."""
    sibling = tmp_path.parent / f"{tmp_path.name}-evil"

    with pytest.raises(PathEscapeError):
        resolve_workspace_path(str(tmp_path), str(sibling))
