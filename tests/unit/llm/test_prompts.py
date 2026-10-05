"""PromptLibrary: versioned Jinja2 templates, loaded by name."""

from pathlib import Path

import jinja2
import pytest

from web_department.llm.prompts import PromptLibrary, PromptNotFound, render


def test_render_picks_the_latest_version(tmp_path: Path) -> None:
    (tmp_path / "greet_v1.jinja2").write_text("Hello v1 {{ name }}")
    (tmp_path / "greet_v2.jinja2").write_text("Hello v2 {{ name }}")
    library = PromptLibrary(tmp_path)

    assert library.render("greet", name="Ada") == "Hello v2 Ada"


def test_render_can_pin_a_specific_version(tmp_path: Path) -> None:
    (tmp_path / "greet_v1.jinja2").write_text("v1")
    (tmp_path / "greet_v2.jinja2").write_text("v2")
    library = PromptLibrary(tmp_path)

    assert library.render("greet", version=1) == "v1"


def test_versions_ignores_unrelated_and_malformed_filenames(tmp_path: Path) -> None:
    (tmp_path / "greet_v1.jinja2").write_text("hi")
    (tmp_path / "other_v1.jinja2").write_text("other")
    (tmp_path / "greet.jinja2").write_text("no version")
    (tmp_path / "greet_v1.txt").write_text("wrong extension")
    library = PromptLibrary(tmp_path)

    assert library.versions("greet") == {1: "greet_v1.jinja2"}


def test_missing_template_raises_prompt_not_found(tmp_path: Path) -> None:
    library = PromptLibrary(tmp_path)

    with pytest.raises(PromptNotFound):
        library.render("nope")


def test_missing_specific_version_raises_with_available_versions(tmp_path: Path) -> None:
    (tmp_path / "greet_v1.jinja2").write_text("hi")
    library = PromptLibrary(tmp_path)

    with pytest.raises(PromptNotFound, match=r"greet v2 \(have: \[1\]\)"):
        library.render("greet", version=2)


def test_undefined_variable_raises(tmp_path: Path) -> None:
    (tmp_path / "greet_v1.jinja2").write_text("Hello {{ name }}")
    library = PromptLibrary(tmp_path)

    with pytest.raises(jinja2.UndefinedError):
        library.render("greet")


def test_default_library_renders_the_real_json_repair_prompt() -> None:
    text = render(
        "json_repair",
        schema='{"type": "object"}',
        error="Expecting value: line 1 column 1",
        previous_response="not json",
    )

    assert '{"type": "object"}' in text
    assert "Expecting value: line 1 column 1" in text
    assert "not json" in text
    assert "ONLY the corrected JSON object" in text
