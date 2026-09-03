"""Transaction style reformat for file mode.

A direct reformat writes each file as its worker finishes it, so a decode
error, a failing custom rule or a disk error later in the batch leaves the
tree half reformatted. The transaction splits the run into a planning
phase, where workers only compute what each file would become, and a
commit phase in the main process: every file is re-checked against the
bytes the workers read, and only then are the changes written, each
through a temp file and an atomic rename. A failure during the commit
restores the files already written, and a file edited again after a
write is left with that edit in place.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from click import ClickException, echo, style

from djlint.lint import linter
from djlint.reformat import reformat_string

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from djlint.settings import Config
    from djlint.types import ProcessResult


class TransactionAborted(ClickException):
    """The transaction could not be committed, so no batch change remains."""

    exit_code = 2


@dataclass(frozen=True, slots=True)
class PlannedFile:
    """One file's part in the batch, ready to commit or roll back.

    ``original`` and ``formatted`` are kept verbatim, with the file's own
    line endings, and the stat snapshot carries the permissions and times
    the committed file has to keep.
    """

    path: Path
    original: str
    formatted: str
    mode: int
    atime_ns: int
    mtime_ns: int
    dev: int
    ino: int


def plan_file(config: Config, this_file: Path) -> ProcessResult:
    """Compute one file's reformat and lint without writing anything.

    Runs in a worker process. The file is read once, verbatim, and when
    linting is on the lint pass runs over the formatted text - the same
    text a direct run would have on disk by the time its lint pass reads
    the file back.
    """
    real_path = this_file.resolve()
    stat_result = real_path.stat()
    original = _read_verbatim(real_path)
    format_message, formatted = reformat_string(
        config, original, str(real_path)
    )

    result: ProcessResult = {"format_message": format_message}
    if config.lint:
        result["lint_message"] = linter(
            config, formatted, str(real_path), real_path.as_posix()
        )
    result["plan"] = PlannedFile(
        path=real_path,
        original=original,
        formatted=formatted,
        mode=stat_result.st_mode,
        atime_ns=stat_result.st_atime_ns,
        mtime_ns=stat_result.st_mtime_ns,
        dev=stat_result.st_dev,
        ino=stat_result.st_ino,
    )
    return result


def dedupe_plans(plans: Iterable[PlannedFile]) -> list[PlannedFile]:
    """Drop plans that target a file another plan already commits.

    Command line arguments are resolved before scanning, so repeats and
    ``..`` style aliases never reach here. A symlink inside a scanned
    directory, or a hard link, still lands on the same inode as its
    target: writing one file through two names would commit it twice and
    make the rollback comparison race itself.
    """
    seen: set[tuple[int, int]] = set()
    unique: list[PlannedFile] = []
    for plan in plans:
        identity = (plan.dev, plan.ino)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(plan)
    return unique


def commit_plan(plans: Sequence[PlannedFile]) -> None:
    """Write every planned change at once, or leave the tree as it was.

    All files are re-read first and compared with what the workers saw:
    a template edited while djLint was still planning aborts the run
    before anything is written. The changes then land one by one through
    atomic renames; a failure mid-way rolls the files already written
    back to their original bytes.
    """
    changed = [plan for plan in plans if plan.formatted != plan.original]

    for plan in changed:
        _assert_unchanged(plan, commit_in_progress=False)

    committed: list[PlannedFile] = []
    try:
        for plan in changed:
            # Re-check right before the rename: the batch validation above
            # does not hold across the first writes.
            _assert_unchanged(plan, commit_in_progress=True)
            try:
                _atomic_write(plan.path, plan.formatted, plan)
            except OSError as error:
                message = (
                    f"Reformat stopped while writing {plan.path}:"
                    f" {error.strerror or error}. Files this run had already"
                    " written were restored; the rest were left untouched."
                )
                raise TransactionAborted(message) from error
            committed.append(plan)
    except TransactionAborted:
        _rollback(committed)
        raise


def _read_verbatim(path: Path) -> str:
    """Read a file as utf-8 without translating its line endings."""
    with path.open(encoding="utf-8", newline="") as stream:
        return stream.read()


def _assert_unchanged(plan: PlannedFile, *, commit_in_progress: bool) -> None:
    """Guard against an external edit landing between planning and commit."""
    try:
        stat_result = plan.path.stat()
        if (stat_result.st_dev, stat_result.st_ino) != (plan.dev, plan.ino):
            tail = (
                " Files this run had already written were restored."
                if commit_in_progress
                else " No files were changed."
            )
            raise TransactionAborted(
                f"Reformat aborted: {plan.path} was replaced after djLint read it."
                + tail
            )
        current = _read_verbatim(plan.path)
    except OSError as error:
        reason = f"it is no longer readable ({error.strerror or error})"
    else:
        if current == plan.original:
            return
        reason = "it was modified after djLint read it"
    tail = (
        " Files this run had already written were restored."
        if commit_in_progress
        else " No files were changed."
    )
    message = f"Reformat aborted: {plan.path} {reason}.{tail}"
    raise TransactionAborted(message)


def _atomic_write(path: Path, text: str, plan: PlannedFile) -> None:
    """Replace ``path`` with ``text``, keeping its permissions and times.

    The bytes land in a temp file in the same directory first, so the
    rename either leaves the original file or the new file in place,
    never a half-written one. Metadata is copied from the file still on
    disk before the rename, and the recorded mode and times are enforced
    on top of it.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".djlint-tmp", dir=path.parent
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        try:
            shutil.copystat(path, tmp_path, follow_symlinks=False)
        except OSError:
            pass
        tmp_path.chmod(plan.mode & 0o7777)
        os.utime(tmp_path, ns=(plan.atime_ns, plan.mtime_ns))
        tmp_path.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise


def _rollback(committed: Sequence[PlannedFile]) -> None:
    """Undo committed writes, best effort, never clobbering external edits."""
    for plan in reversed(committed):
        try:
            current = _read_verbatim(plan.path)
        except OSError as error:
            echo(
                style(
                    f"Warning: could not check {plan.path} while rolling back"
                    f" ({error.strerror or error}); leaving it as written.",
                    fg="yellow",
                ),
                err=True,
            )
            continue
        if current != plan.formatted:
            echo(
                style(
                    f"Warning: {plan.path} was edited after djLint wrote it;"
                    " keeping those edits instead of rolling the file back.",
                    fg="yellow",
                ),
                err=True,
            )
            continue
        try:
            _atomic_write(plan.path, plan.original, plan)
        except OSError as error:
            echo(
                style(
                    f"Warning: could not restore {plan.path} while rolling"
                    f" back ({error.strerror or error}); it keeps the"
                    " formatted text.",
                    fg="yellow",
                ),
                err=True,
            )
