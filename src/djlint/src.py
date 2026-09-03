"""Build src file list."""

from __future__ import annotations

import fnmatch
import os
import sys
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

import regex as re
from click import echo, style

if sys.version_info >= (3, 13):
    from typing import NamedTuple
else:
    from typing_extensions import NamedTuple

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from typing import Final

    from djlint.settings import Config


class SrcFiles(NamedTuple):
    """Files to process, and why the list may be empty.

    An empty ``paths`` with ``excluded`` set means candidates were found
    and every one of them was deliberately skipped by the configuration -
    a successful no-op. An empty ``paths`` without it means nothing
    matched the requested paths at all, which is a usage error.
    """

    paths: list[Path]
    excluded: bool


class WorkspaceFiles(NamedTuple):
    """Files to process in workspace mode, each with its effective config.

    ``excluded`` carries the same meaning as in :class:`SrcFiles`.
    """

    entries: list[tuple[Path, Config]]
    excluded: bool


def _gitignore_match(config: Config, filepath: Path) -> bool:
    """Check if a file matches gitignore patterns using a relative path.

    pathspec.match_file matches against all path components, so passing
    an absolute path causes false positives when parent directories
    (outside the project) match a gitignore pattern. In workspace mode the
    config also carries .gitignore files found in nested config scopes,
    each matched relative to the directory it lives in.
    """
    for base, spec in (
        (config.project_root, config.gitignore),
        *config.gitignore_scopes,
    ):
        try:
            rel = filepath.relative_to(base)
        except ValueError:
            continue
        if spec.match_file(rel):
            return True
    return False


def _exclude_match(config: Config, filepath: Path, root: Path) -> bool:
    """Check if a file matches exclude patterns using relative paths."""
    relative_roots = (
        (config.project_root,)
        if config.project_root == root
        else (config.project_root, root)
    )
    for relative_root in relative_roots:
        try:
            rel = filepath.relative_to(relative_root)
        except ValueError:
            continue
        rel_path = rel.as_posix()
        if config.exclude_pattern.search(rel_path):
            return True
        if (
            relative_root == config.project_root
            and config.exclude_pattern.search(f"/{rel_path}")
        ):
            return True
    return False


def _included(config: Config, filepath: Path) -> bool:
    """Check a file against the filters that need it to exist on disk."""
    if config.workspace and config.files:
        relative = filepath.relative_to(config.project_root).as_posix()
        if not any(
            fnmatch.fnmatch(relative, str(pattern))
            or fnmatch.fnmatch(filepath.name, str(pattern))
            for pattern in config.files
        ):
            return False
    return _has_required_pragma(config, filepath) and (
        not config.use_gitignore or not _gitignore_match(config, filepath)
    )


def get_src(src: Iterable[Path], config: Config) -> SrcFiles:
    """Get source files."""
    paths: dict[Path, None] = {}
    excluded = False
    for item in src:
        normalized_item = item.resolve()

        if normalized_item.is_file():
            candidates: Iterable[Path] = (normalized_item,)
            exclude_root = config.project_root
        else:
            extension = config.extension.removeprefix(".")
            candidates = normalized_item.glob(f"**/*.{extension}")
            exclude_root = normalized_item

        for candidate in candidates:
            if candidate in paths:
                continue
            if _exclude_match(config, candidate, exclude_root):
                excluded = True
            elif candidate.is_file():
                if _included(config, candidate):
                    paths[candidate] = None
                else:
                    excluded = True

    return SrcFiles(list(paths), excluded)


def get_workspace_src(
    src: Iterable[Path],
    build_scope: Callable[[Path, Path], Config],
) -> WorkspaceFiles:
    """Get source files with per-directory configuration.

    Each directory argument is a workspace boundary. Files found under it
    are paired with the config resolved layer by layer from that boundary
    down to their own directory; config files above the boundary are not
    read. Discovery (extension, exclude, gitignore, pragma) and the later
    lint/reformat run use the same per-file config, and overlapping input
    paths are processed once.

    Every scope config is built while walking the tree, before any file is
    linted or reformatted, so an invalid config or rules file fails the
    run here, with the offending file named.
    """
    paths: dict[Path, Config] = {}
    excluded = False
    items = [item.resolve() for item in src]
    roots = [item for item in items if item.is_dir()]
    explicit_files = [item for item in items if item.is_file()]
    scope_cache: dict[tuple[Path, Path], Config] = {}

    def scope_for(boundary: Path, directory: Path) -> Config:
        key = (boundary, directory)
        cached = scope_cache.get(key)
        if cached is None:
            cached = build_scope(boundary, directory)
            scope_cache[key] = cached
        return cached

    def consider(candidate: Path, this_config: Config, root: Path) -> bool:
        """Add a candidate with its config; return True if it was skipped."""
        if candidate in paths:
            return False
        if _exclude_match(this_config, candidate, root):
            return True
        if not candidate.is_file():
            return False
        if _included(this_config, candidate):
            paths[candidate] = this_config
            return False
        return True

    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            directory = Path(dirpath)
            scope_error: Exception | None = None
            try:
                this_config = scope_for(root, directory)
            except Exception as error:
                if directory == root:
                    raise
                scope_error = error
                this_config = scope_for(root, directory.parent)
            extension = "." + this_config.extension.removeprefix(".")

            kept_dirs: list[str] = []
            for name in dirnames:
                child = directory / name
                if _exclude_match(
                    this_config, child, root
                ) or (this_config.use_gitignore and _gitignore_match(
                    this_config, child
                )):
                    # A skipped tree still counts as "candidates the
                    # configuration deliberately skipped" when it holds
                    # anything, keeping the empty-list result at success.
                    if any(child.iterdir()):
                        excluded = True
                else:
                    kept_dirs.append(name)
            dirnames[:] = kept_dirs

            for name in filenames:
                if not name.endswith(extension):
                    continue
                candidate = directory / name
                if scope_error is not None:
                    raise scope_error
                excluded = consider(candidate, this_config, root) or excluded

    for this_file in explicit_files:
        if this_file in paths:
            continue
        boundary = next(
            (
                root
                for root in reversed(roots)
                if root == this_file or root in this_file.parents
            ),
            None,
        )
        if boundary is None:
            cwd = Path.cwd()
            boundary = (
                cwd
                if cwd == this_file.parent or cwd in this_file.parents
                else this_file.parent
            )
        this_config = scope_for(boundary, this_file.parent)
        excluded = consider(this_file, this_config, boundary) or excluded

    return WorkspaceFiles(list(paths.items()), excluded)


def print_no_files_to_check(*, excluded: bool) -> None:
    """Report an empty file list on stderr.

    Never on stdout: that is where formatted code is written, and a
    diagnostic mixed into it would be read back as file contents.
    """
    message = "No files to check! 😢"
    if excluded:
        message += " Everything that matched was skipped by the configuration."
    echo(style(message, fg="blue"), err=True)


_HTML_PRAGMA_PATTERNS: Final = (
    re.compile(r"<!--\s*djlint\:on\s*-->", cache_pattern=False),
)
_TEMPLATE_COMMENT_PRAGMA_PATTERN: Final = re.compile(
    r"\{#\s*djlint\:on\s*#\}", cache_pattern=False
)
_DJANGO_JINJA_PRAGMA_PATTERNS: Final = (
    _TEMPLATE_COMMENT_PRAGMA_PATTERN,
    re.compile(
        r"\{%\s*comment\s*%\}\s*djlint\:on\s*\{%\s*endcomment\s*%\}",
        cache_pattern=False,
    ),
)
_NUNJUCKS_PRAGMA_PATTERNS: Final = (_TEMPLATE_COMMENT_PRAGMA_PATTERN,)
_HANDLEBARS_PRAGMA_PATTERNS: Final = (
    re.compile(r"\{\{!--\s*djlint\:on\s*--\}\}", cache_pattern=False),
)
_GOLANG_PRAGMA_PATTERNS: Final = (
    re.compile(
        r"\{\{-?\s*/\*\s*djlint\:on\s*\*/\s*-?\}\}", cache_pattern=False
    ),
)
_PRAGMA_PATTERNS: Final = MappingProxyType({
    "html": _HTML_PRAGMA_PATTERNS,
    "django": _DJANGO_JINJA_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "jinja": _DJANGO_JINJA_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "askama": _DJANGO_JINJA_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "tera": _DJANGO_JINJA_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "liquid": _DJANGO_JINJA_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "nunjucks": _NUNJUCKS_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "handlebars": _HANDLEBARS_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "golang": _GOLANG_PRAGMA_PATTERNS + _HTML_PRAGMA_PATTERNS,
    "angular": _HTML_PRAGMA_PATTERNS,
    "all": _DJANGO_JINJA_PRAGMA_PATTERNS
    + _NUNJUCKS_PRAGMA_PATTERNS
    + _HANDLEBARS_PRAGMA_PATTERNS
    + _GOLANG_PRAGMA_PATTERNS
    + _HTML_PRAGMA_PATTERNS,
})


def has_pragma(config: Config, first_line: str) -> bool:
    """Check whether a line enables djLint."""
    for pattern in _PRAGMA_PATTERNS[config.profile]:
        if pattern.match(first_line):
            return True
    return False


def _has_required_pragma(config: Config, this_file: Path) -> bool:
    """Whether the file opens with the pragma, when one is required.

    The pragma is ascii, so a byte the file's first line cannot decode
    is not it, and is reported when the file itself is read.
    """
    if not config.require_pragma:
        return True

    with this_file.open(encoding="utf-8", errors="replace") as open_file:
        first_line = open_file.readline()

    return has_pragma(config, first_line)
