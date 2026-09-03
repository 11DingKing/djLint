"""Tests for workspace config mode.

With ``--workspace`` each file is linted and reformatted with the config
resolved layer by layer from the given path (the workspace boundary) down
to the file's own directory; config above the boundary is not used.

uv run pytest tests/test_config/test_workspace/test_workspace.py
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from djlint import main as djlint

if TYPE_CHECKING:
    from pathlib import Path

    from click.testing import CliRunner


def _pyproject(directory: Path, body: str) -> None:
    (directory / "pyproject.toml").write_text(
        f"[tool.djlint]\n{body}", encoding="utf-8"
    )


def _file_section(output: str, filename: str) -> str:
    """The report block printed for one file, or "" if it is absent."""
    marker = f"\n{filename}\n"
    start = output.find(marker)
    if start == -1:
        return ""
    block = output[start + len(marker):]
    end = block.find("\n\n")
    return block if end == -1 else block[:end]


_CUSTOM_RULE = """\
- rule:
    name: {code}
    message: Scope marker {code}
    patterns:
      - "{mark}"
"""


def test_profiles_and_extensions_resolve_per_directory(
    runner: CliRunner, tmp_path: Path
) -> None:
    web = tmp_path / "web"
    mail = tmp_path / "mail"
    web.mkdir()
    mail.mkdir()
    _pyproject(web, 'profile = "django"\n')
    _pyproject(mail, 'profile = "golang"\nextension = "tmpl"\n')
    (web / "page.html").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "go.tmpl").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "skip.html").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--lint"))

    assert result.exit_code == 1
    assert "Linting 2/2 files" in result.output
    assert "T001" in _file_section(result.output, "web/page.html")
    # the golang scope does not run T001, and only it looks for .tmpl
    assert "mail/skip.html" not in result.output
    assert "T001" not in _file_section(result.output, "mail/go.tmpl")


def test_layers_inherit_and_nearest_layer_wins(
    runner: CliRunner, tmp_path: Path
) -> None:
    _pyproject(tmp_path, 'ignore = "H025"\n')
    web = tmp_path / "web"
    api = tmp_path / "api"
    web.mkdir()
    api.mkdir()
    _pyproject(web, 'profile = "django"\n')
    _pyproject(api, 'ignore = ""\n')
    (web / "a.html").write_text("</div>\n", encoding="utf-8")
    (api / "b.html").write_text("</div>\n", encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--lint"))

    assert result.exit_code == 1
    # web inherits the root ignore; api's own layer clears it
    assert "web/a.html" not in result.output
    assert "H025" in _file_section(result.output, "api/b.html")


def test_nested_directories_inherit_root_layers(
    runner: CliRunner, tmp_path: Path
) -> None:
    _pyproject(tmp_path, 'profile = "django"\n')
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (deep / "x.html").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--lint"))

    assert result.exit_code == 1
    assert "a/b/c/x.html" in result.output
    assert "T001" in result.output


def test_exclude_is_scoped(runner: CliRunner, tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "old").mkdir(parents=True)
    mail = tmp_path / "mail"
    (mail / "old").mkdir(parents=True)
    _pyproject(web, 'profile = "django"\nextend_exclude = "old"\n')
    _pyproject(mail, 'profile = "golang"\n')
    (web / "old" / "a.html").write_text("{{foo}}\n", encoding="utf-8")
    (web / "new.html").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "old" / "b.html").write_text('<img src="x">\n', encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--lint"))

    assert result.exit_code == 1
    assert "web/old" not in result.output
    assert "web/new.html" in result.output
    assert "mail/old/b.html" in result.output


def test_gitignore_is_scoped(runner: CliRunner, tmp_path: Path) -> None:
    web = tmp_path / "web"
    (web / "sub").mkdir(parents=True)
    mail = tmp_path / "mail"
    mail.mkdir()
    _pyproject(web, 'profile = "django"\nuse_gitignore = true\n')
    _pyproject(mail, 'profile = "golang"\n')
    (web / ".gitignore").write_text("secret.html\n", encoding="utf-8")
    (web / "sub" / ".gitignore").write_text("nested.html\n", encoding="utf-8")
    (web / "secret.html").write_text("{{foo}}\n", encoding="utf-8")
    (web / "public.html").write_text("{{foo}}\n", encoding="utf-8")
    (web / "sub" / "nested.html").write_text("{{foo}}\n", encoding="utf-8")
    (web / "sub" / "visible.html").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "secret.html").write_text('<img src="x">\n', encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--lint"))

    assert result.exit_code == 1
    assert "Linting 3/3 files" in result.output
    assert "web/secret.html" not in result.output
    assert "web/sub/nested.html" not in result.output
    assert "web/public.html" in result.output
    assert "web/sub/visible.html" in result.output
    assert "mail/secret.html" in result.output


def test_custom_rules_are_scoped(runner: CliRunner, tmp_path: Path) -> None:
    web = tmp_path / "web"
    mail = tmp_path / "mail"
    web.mkdir()
    mail.mkdir()
    (web / ".djlint_rules.yaml").write_text(
        _CUSTOM_RULE.format(code="X901", mark="WEB_MARK"), encoding="utf-8"
    )
    (mail / ".djlint_rules.yaml").write_text(
        _CUSTOM_RULE.format(code="Y901", mark="MAIL_MARK"), encoding="utf-8"
    )
    (web / "a.html").write_text("WEB_MARK and MAIL_MARK\n", encoding="utf-8")
    (mail / "b.html").write_text("WEB_MARK and MAIL_MARK\n", encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--lint"))

    assert result.exit_code == 1
    web_section = _file_section(result.output, "web/a.html")
    mail_section = _file_section(result.output, "mail/b.html")
    assert "X901" in web_section
    assert "Y901" not in web_section
    assert "Y901" in mail_section
    assert "X901" not in mail_section


def test_cli_options_override_every_scope(runner: CliRunner, tmp_path: Path) -> None:
    web = tmp_path / "web"
    mail = tmp_path / "mail"
    web.mkdir()
    mail.mkdir()
    _pyproject(web, 'profile = "django"\n')
    _pyproject(mail, 'profile = "django"\n')
    (web / "a.html").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "b.html").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(
        djlint,
        (str(tmp_path), "--workspace", "--lint", "--profile", "golang"),
    )

    assert result.exit_code == 0
    assert "T001" not in result.output


def test_configuration_precedence_stays_clear(
    runner: CliRunner, tmp_path: Path
) -> None:
    web = tmp_path / "web"
    web.mkdir()
    _pyproject(web, 'profile = "django"\nignore = "H025"\n')
    (web / "a.html").write_text("{{foo}}\n", encoding="utf-8")
    global_config = tmp_path / "global.toml"
    global_config.write_text('ignore = "T001"\n', encoding="utf-8")

    # the project layer wins over the global file by default
    result = runner.invoke(
        djlint,
        (
            str(tmp_path),
            "--workspace",
            "--lint",
            "--configuration",
            str(global_config),
        ),
    )
    assert result.exit_code == 1
    assert "T001" in result.output

    # --prefer-configuration turns that around
    result = runner.invoke(
        djlint,
        (
            str(tmp_path),
            "--workspace",
            "--lint",
            "--configuration",
            str(global_config),
            "--prefer-configuration",
        ),
    )
    assert result.exit_code == 0
    assert "T001" not in result.output


def test_overlapping_inputs_are_processed_once(
    runner: CliRunner, tmp_path: Path
) -> None:
    web = tmp_path / "web"
    web.mkdir()
    _pyproject(web, 'profile = "django"\n')
    (web / "a.html").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(
        djlint, (str(tmp_path), str(web), "--workspace", "--check")
    )

    assert "1/1 files" in result.output
    assert result.output.count("web/a.html") >= 1


def test_invalid_config_fails_before_anything_is_modified(
    runner: CliRunner, tmp_path: Path
) -> None:
    good = tmp_path / "good"
    bad = tmp_path / "bad"
    good.mkdir()
    bad.mkdir()
    original = "<div><p>x</p>   </div>\n"
    (good / "a.html").write_text(original, encoding="utf-8")
    _pyproject(bad, 'profile = "nonsense"\n')
    (bad / "broken.html").write_text(original, encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--reformat"))

    assert result.exit_code == 2
    assert "nonsense" in result.output
    assert (good / "a.html").read_text(encoding="utf-8") == original

    # a broken config file is reported with its own path
    _pyproject(bad, 'profile = "django\n')
    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--reformat"))

    assert result.exit_code == 2
    assert "bad/pyproject.toml" in result.output
    assert (good / "a.html").read_text(encoding="utf-8") == original


def test_invalid_rules_fail_before_anything_is_modified(
    runner: CliRunner, tmp_path: Path
) -> None:
    web = tmp_path / "web"
    web.mkdir()
    original = "<div><p>x</p></div>\n"
    (web / "a.html").write_text(original, encoding="utf-8")
    (web / ".djlint_rules.yaml").write_text(
        '- rule:\n    name: X901\n    patterns:\n      - "MARK"\n',
        encoding="utf-8",  # rule is missing a message
    )

    result = runner.invoke(djlint, (str(tmp_path), "--workspace", "--reformat"))

    assert result.exit_code == 2
    assert ".djlint_rules.yaml" in result.output
    assert (web / "a.html").read_text(encoding="utf-8") == original


def test_without_workspace_keeps_legacy_behavior(
    runner: CliRunner, tmp_path: Path
) -> None:
    web = tmp_path / "web"
    mail = tmp_path / "mail"
    web.mkdir()
    mail.mkdir()
    _pyproject(web, 'profile = "django"\n')
    _pyproject(mail, 'profile = "golang"\nextension = "tmpl"\n')
    (web / "page.html").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "go.tmpl").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--lint"))

    assert "Linting 1/1 files" in result.output
    assert "T001" not in result.output


def test_stdin_workflow_is_unchanged(runner: CliRunner) -> None:
    result = runner.invoke(
        djlint, ("-", "--workspace", "--lint"), input="<div></div>"
    )

    assert result.exit_code == 1
    assert "Linted 1 file" in result.output
    assert "H020" in result.output


def test_single_project_workspace_matches_normal_run(
    runner: CliRunner, tmp_path: Path
) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    _pyproject(proj, 'profile = "django"\n')
    (proj / "a.html").write_text("{{foo}}\n", encoding="utf-8")

    normal = runner.invoke(djlint, (str(proj), "--lint"))
    workspace = runner.invoke(djlint, (str(proj), "--workspace", "--lint"))

    assert normal.exit_code == 1
    assert workspace.exit_code == 1
    assert "T001" in normal.output
    assert "T001" in workspace.output
    assert "1/1 files" in workspace.output


def test_workspace_can_be_enabled_in_config(
    runner: CliRunner, tmp_path: Path
) -> None:
    _pyproject(tmp_path, "workspace = true\n")
    web = tmp_path / "web"
    mail = tmp_path / "mail"
    web.mkdir()
    mail.mkdir()
    _pyproject(web, 'profile = "django"\n')
    _pyproject(mail, 'profile = "golang"\nextension = "tmpl"\n')
    (web / "page.html").write_text("{{foo}}\n", encoding="utf-8")
    (mail / "go.tmpl").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(djlint, (str(tmp_path), "--lint"))

    assert "Linting 2/2 files" in result.output


def test_github_output_uses_scoped_paths(runner: CliRunner, tmp_path: Path) -> None:
    web = tmp_path / "web"
    web.mkdir()
    _pyproject(web, 'profile = "django"\n')
    (web / "a.html").write_text("{{foo}}\n", encoding="utf-8")

    result = runner.invoke(
        djlint,
        (str(tmp_path), "--workspace", "--lint", "--github-output"),
        env={"GITHUB_ACTIONS": ""},
    )

    assert "::warning file=web/a.html" in result.output
