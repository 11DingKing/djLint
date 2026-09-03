"""Tests for transactional reformat.

uv run pytest tests/test_djlint/test_transactional.py
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from djlint import main as djlint, transaction
from djlint.settings import Config

if TYPE_CHECKING:
    from click.testing import CliRunner

    from djlint.transaction import PlannedFile

DIRTY_A = "<div><p>x</p>   </div>\n"
DIRTY_B = "<div>\n   <p>y</p>\n</div>\n"
FORMATTED_A = "<div>\n    <p>x</p>\n</div>\n"
FORMATTED_B = "<div>\n    <p>y</p>\n</div>\n"


def _project(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[tool]\n[tool.djlint]\n", encoding="utf-8"
    )


def _plans(config: Config, *files: Path) -> list[PlannedFile]:
    """Plan the given files, with alias paths collapsed to one inode."""
    return transaction.dedupe_plans(
        result["plan"]
        for result in (transaction.plan_file(config, f) for f in files)
    )


def test_transactional_reformat_commits_batch(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The whole batch lands once every file plans cleanly."""
    _project(tmp_path)
    (tmp_path / "a.html").write_text(DIRTY_A, encoding="utf-8")
    (tmp_path / "b.html").write_text(DIRTY_B, encoding="utf-8")

    result = runner.invoke(
        djlint, (str(tmp_path), "--reformat", "--transactional")
    )

    assert result.exit_code == 1
    assert "2 files were updated." in result.output
    assert (tmp_path / "a.html").read_text(encoding="utf-8") == FORMATTED_A
    assert (tmp_path / "b.html").read_text(encoding="utf-8") == FORMATTED_B


def test_transactional_clean_run(runner: CliRunner, tmp_path: Path) -> None:
    """A batch with nothing to change stays clean and exits 0."""
    _project(tmp_path)
    (tmp_path / "a.html").write_text(FORMATTED_A, encoding="utf-8")

    result = runner.invoke(
        djlint, (str(tmp_path), "--reformat", "--transactional")
    )

    assert result.exit_code == 0
    assert "0 files were updated." in result.output


def test_undecodable_file_aborts_whole_batch(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A worker failure lands before the commit, so no file is touched."""
    _project(tmp_path)
    good = tmp_path / "a.html"
    good.write_text(DIRTY_A, encoding="utf-8")
    (tmp_path / "bad.html").write_bytes(b"\xff\xfe<div>")

    result = runner.invoke(
        djlint, (str(tmp_path), "--reformat", "--transactional")
    )

    assert result.exit_code == 2
    assert good.read_text(encoding="utf-8") == DIRTY_A
    assert "UnicodeDecodeError" in result.output


def test_externally_modified_file_aborts_commit(tmp_path: Path) -> None:
    """An edit between planning and commit aborts before anything is written."""
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    b.write_text(DIRTY_B, encoding="utf-8")
    plans = _plans(Config(str(tmp_path), reformat=True), a, b)

    b.write_text("<div>sneaky editor</div>\n", encoding="utf-8")

    with pytest.raises(transaction.TransactionAborted, match="modified after"):
        transaction.commit_plan(plans)
    assert a.read_text(encoding="utf-8") == DIRTY_A
    assert b.read_text(encoding="utf-8") == "<div>sneaky editor</div>\n"


def test_write_failure_rolls_back_committed_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing write restores the files the transaction already replaced."""
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    b.write_text(DIRTY_B, encoding="utf-8")
    plans = _plans(Config(str(tmp_path), reformat=True), a, b)

    real_write = transaction._atomic_write  # noqa: SLF001
    attempts = 0

    def failing_write(path: Path, text: str, plan: PlannedFile) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise OSError(28, "No space left on device")
        real_write(path, text, plan)

    monkeypatch.setattr(transaction, "_atomic_write", failing_write)

    with pytest.raises(transaction.TransactionAborted, match="stopped while"):
        transaction.commit_plan(plans)
    assert a.read_text(encoding="utf-8") == DIRTY_A
    assert b.read_text(encoding="utf-8") == DIRTY_B


def test_rollback_keeps_external_edits(tmp_path: Path) -> None:
    """A file edited after our write is not overwritten by the rollback."""
    a = tmp_path / "a.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    plan = _plans(Config(str(tmp_path), reformat=True), a)[0]

    transaction._atomic_write(a, plan.formatted, plan)  # noqa: SLF001
    a.write_text(plan.formatted + "<!-- editor -->\n", encoding="utf-8")
    transaction._rollback([plan])  # noqa: SLF001

    assert a.read_text(encoding="utf-8").endswith("<!-- editor -->\n")

    transaction._atomic_write(a, plan.formatted, plan)  # noqa: SLF001
    transaction._rollback([plan])  # noqa: SLF001

    assert a.read_text(encoding="utf-8") == DIRTY_A


def test_deleted_file_aborts_commit(tmp_path: Path) -> None:
    """A file gone between planning and commit aborts like a change would."""
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    b.write_text(DIRTY_B, encoding="utf-8")
    plans = _plans(Config(str(tmp_path), reformat=True), a, b)

    b.unlink()

    with pytest.raises(
        transaction.TransactionAborted, match="no longer readable"
    ):
        transaction.commit_plan(plans)
    assert a.read_text(encoding="utf-8") == DIRTY_A


def test_copystat_failure_still_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A metadata copy failure falls back to the recorded mode and times."""
    a = tmp_path / "a.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    a.chmod(0o600)
    plans = _plans(Config(str(tmp_path), reformat=True), a)

    def failing_copystat(*_args: object, **_kwargs: object) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(shutil, "copystat", failing_copystat)

    transaction.commit_plan(plans)
    assert a.read_text(encoding="utf-8") == FORMATTED_A
    assert a.stat().st_mode & 0o7777 == 0o600


def test_atomic_write_cleans_its_temp_file_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure before the rename leaves no temp file behind."""
    a = tmp_path / "a.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    plan = _plans(Config(str(tmp_path), reformat=True), a)[0]

    def failing_utime(*_args: object, **_kwargs: object) -> None:
        raise OSError(22, "Invalid argument")

    monkeypatch.setattr(os, "utime", failing_utime)

    with pytest.raises(OSError, match="Invalid argument"):
        transaction._atomic_write(a, plan.formatted, plan)  # noqa: SLF001

    assert a.read_text(encoding="utf-8") == DIRTY_A
    assert not list(tmp_path.glob("*.djlint-tmp"))


def test_external_modification_during_commit_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An edit landing after validation still aborts, undoing earlier writes."""
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    b.write_text(DIRTY_B, encoding="utf-8")
    plans = _plans(Config(str(tmp_path), reformat=True), a, b)

    real_read = transaction._read_verbatim  # noqa: SLF001
    reads_by_name: dict[str, int] = {}

    def tampered_on_recheck(path: Path) -> str:
        count = reads_by_name.get(path.name, 0) + 1
        reads_by_name[path.name] = count
        if path.name == "b.html" and count >= 2:
            return "<div>editor again</div>\n"
        return real_read(path)

    monkeypatch.setattr(transaction, "_read_verbatim", tampered_on_recheck)

    with pytest.raises(transaction.TransactionAborted, match="restored"):
        transaction.commit_plan(plans)
    assert a.read_text(encoding="utf-8") == DIRTY_A
    assert b.read_text(encoding="utf-8") == DIRTY_B


def test_rollback_warns_when_file_gone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A committed file deleted before rollback is reported, not crashed on."""
    a = tmp_path / "a.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    plan = _plans(Config(str(tmp_path), reformat=True), a)[0]
    transaction._atomic_write(a, plan.formatted, plan)  # noqa: SLF001
    a.unlink()

    transaction._rollback([plan])  # noqa: SLF001

    assert "could not check" in capsys.readouterr().err


def test_rollback_warns_when_restore_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A failing restore leaves the formatted content and keeps rolling."""
    a = tmp_path / "a.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    plan = _plans(Config(str(tmp_path), reformat=True), a)[0]
    transaction._atomic_write(a, plan.formatted, plan)  # noqa: SLF001

    def failing_write(*_args: object, **_kwargs: object) -> None:
        raise OSError(5, "I/O error")

    monkeypatch.setattr(transaction, "_atomic_write", failing_write)

    transaction._rollback([plan])  # noqa: SLF001

    assert "could not restore" in capsys.readouterr().err
    assert a.read_text(encoding="utf-8") == plan.formatted


def test_permissions_times_and_line_endings_preserved(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Mode, mtime and CRLF line endings survive the committed write."""
    _project(tmp_path)
    the_file = tmp_path / "a.html"
    the_file.write_bytes(b"<div><p>x</p>   </div>\r\n")
    the_file.chmod(0o640)
    os.utime(the_file, ns=(1234, 1234))

    result = runner.invoke(
        djlint, (str(tmp_path), "--reformat", "--transactional")
    )

    assert result.exit_code == 1
    stat_result = the_file.stat()
    assert stat_result.st_mode & 0o7777 == 0o640
    assert stat_result.st_mtime_ns == 1234
    content = the_file.read_bytes()
    assert b"\r\n" in content
    assert b"\r" not in content.replace(b"\r\n", b"")


def test_symlink_alias_is_committed_once(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink inside the batch resolves to one inode and writes it once."""
    _project(tmp_path)
    a = tmp_path / "a.html"
    b = tmp_path / "b.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    b.write_text(DIRTY_B, encoding="utf-8")
    try:
        (tmp_path / "link.html").symlink_to(a)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not supported on this platform")

    replaced: list[str] = []
    real_replace = Path.replace

    def spy_replace(self: Path, target: str | Path) -> Path:
        replaced.append(Path(target).name)
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", spy_replace)

    result = runner.invoke(
        djlint, (str(tmp_path), "--reformat", "--transactional")
    )

    assert result.exit_code == 1
    assert sorted(replaced) == ["a.html", "b.html"]
    assert "2 files were updated." in result.output
    assert (tmp_path / "link.html").read_text(encoding="utf-8") == FORMATTED_A


def test_lint_sees_planned_formatted_content(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The same-run lint inspects the formatted text, as a direct run does."""
    source = DIRTY_A + '<img src="x">\n'
    direct_dir = tmp_path / "direct"
    transactional_dir = tmp_path / "transactional"
    for directory in (direct_dir, transactional_dir):
        directory.mkdir()
        (directory / "pyproject.toml").write_text(
            "[tool]\n[tool.djlint]\n", encoding="utf-8"
        )
        (directory / "a.html").write_text(source, encoding="utf-8")

    direct = runner.invoke(djlint, (str(direct_dir), "--reformat", "--lint"))
    transactional = runner.invoke(
        djlint,
        (str(transactional_dir), "--reformat", "--lint", "--transactional"),
    )

    assert direct.exit_code == transactional.exit_code
    assert direct.output.replace(str(direct_dir), "ROOT") == (
        transactional.output.replace(str(transactional_dir), "ROOT")
    )
    assert (direct_dir / "a.html").read_text(encoding="utf-8") == (
        transactional_dir / "a.html"
    ).read_text(encoding="utf-8")


def test_transactional_ignored_for_check(
    runner: CliRunner, tmp_path: Path
) -> None:
    """Check mode never writes; the flag cannot change that or the output."""
    _project(tmp_path)
    the_file = tmp_path / "a.html"
    the_file.write_text(DIRTY_A, encoding="utf-8")

    result = runner.invoke(
        djlint, (str(tmp_path), "--check", "--transactional")
    )

    assert result.exit_code == 1
    assert "1 file would be updated." in result.output
    assert the_file.read_text(encoding="utf-8") == DIRTY_A


def test_transactional_rejects_stdin_before_reading(runner: CliRunner) -> None:
    """The transaction is a file-mode feature and rejects stdin use."""
    result = runner.invoke(
        djlint, ("-", "--reformat", "--transactional"), input=DIRTY_A
    )

    assert result.exit_code == 2
    assert "only available for file reformatting" in result.output
    assert result.output.count("<div>") == 0


def test_replaced_inode_aborts_commit(tmp_path: Path) -> None:
    """Replacing a file with identical bytes still invalidates its snapshot."""
    a = tmp_path / "a.html"
    a.write_text(DIRTY_A, encoding="utf-8")
    plan = _plans(Config(str(tmp_path), reformat=True), a)[0]

    replacement = tmp_path / "replacement.html"
    replacement.write_text(DIRTY_A, encoding="utf-8")
    replacement.replace(a)

    with pytest.raises(transaction.TransactionAborted, match="replaced"):
        transaction.commit_plan([plan])
    assert a.read_text(encoding="utf-8") == DIRTY_A


def test_transactional_config_setting(tmp_path: Path) -> None:
    """The option is reachable from pyproject as well as the command line."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.djlint]\ntransactional=true\n", encoding="utf-8"
    )

    assert Config(str(tmp_path), reformat=True).transactional is True
    assert Config("dummy/source.html").transactional is False
