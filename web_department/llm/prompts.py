"""Versioned Jinja2 prompt templates, loaded by name.

Files live in ``prompts/`` and are named ``<name>_v<N>.jinja2``; ``render(name)`` picks the
highest ``N`` unless a specific ``version`` is given, so a new version can be added without
breaking callers pinned to an older one.
"""

import re
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

DEFAULT_PROMPTS_DIR = Path(__file__).parent / "prompts"
_FILENAME = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)_v(?P<version>\d+)\.jinja2$")


class PromptNotFound(Exception):
    """Raised when no template file matches the requested name (and version, if given)."""


class PromptLibrary:
    """Loads versioned prompt templates from a directory."""

    def __init__(self, directory: Path = DEFAULT_PROMPTS_DIR) -> None:
        self._directory = directory
        self._env = Environment(
            loader=FileSystemLoader(directory),
            undefined=StrictUndefined,
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def versions(self, name: str) -> dict[int, str]:
        """Map version number to filename for every ``<name>_v<N>.jinja2`` file."""
        found: dict[int, str] = {}
        for path in self._directory.glob(f"{name}_v*.jinja2"):
            match = _FILENAME.match(path.name)
            if match and match["name"] == name:
                found[int(match["version"])] = path.name
        return found

    def render(self, name: str, /, version: int | None = None, **context: Any) -> str:
        """Render ``name`` (latest version, or a specific ``version``) with ``context``."""
        versions = self.versions(name)
        if not versions:
            raise PromptNotFound(name)
        chosen = version if version is not None else max(versions)
        if chosen not in versions:
            raise PromptNotFound(f"{name} v{chosen} (have: {sorted(versions)})")
        return self._env.get_template(versions[chosen]).render(**context)


_default_library = PromptLibrary()


def render(name: str, /, version: int | None = None, **context: Any) -> str:
    """Render a prompt from ``web_department/llm/prompts/`` by name; see ``PromptLibrary``."""
    return _default_library.render(name, version=version, **context)
