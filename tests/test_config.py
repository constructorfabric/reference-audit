"""The per-user config file: where it is, its priority, and its strictness."""

from __future__ import annotations

import pytest

from reference_audit.config import AuditConfig, UserConfigError, user_config_file


@pytest.fixture
def user_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    path = tmp_path / "xdg" / "reference-audit" / "config.toml"
    path.parent.mkdir(parents=True)
    for var in ("OPENAI_API_KEY", "S2_API_KEY", "MODEL", "USE_LLM", "CLICKHOUSE_PORT",
                "SOURCE_BACKEND", "CLICKHOUSE_PASSWORD", "CLICKHOUSE_DEFAULT_USER_PASSWORD",
                "OPENALEX_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    return path


def test_user_config_file_follows_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert user_config_file() == tmp_path / "reference-audit" / "config.toml"


def test_user_config_file_ignores_relative_xdg_config_home(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert user_config_file() == tmp_path / ".config" / "reference-audit" / "config.toml"


def test_values_are_read_by_field_name_and_typed(user_config):
    user_config.write_text(
        'openai_api_key = "user-key"\nuse_llm = false\nclickhouse_port = 9000\n'
        'source_backend = "clickhouse"\nclickhouse_password = "pw"\n'
    )
    config = AuditConfig(_env_file=None)
    assert config.openai_api_key == "user-key"
    assert config.use_llm is False
    assert config.clickhouse_port == 9000
    assert config.source_backend == "clickhouse"
    assert config.clickhouse_password == "pw"


def test_project_env_overrides_user_config_and_environment_overrides_both(
        user_config, monkeypatch, tmp_path):
    user_config.write_text('openai_api_key = "user-key"\ns2_api_key = "user-s2"\n'
                           'model = "user-model"\nclickhouse_password = "user-pw"\n')
    project = tmp_path / "project.env"
    project.write_text("S2_API_KEY=project-s2\nMODEL=project-model\n")
    monkeypatch.setenv("MODEL", "env-model")
    monkeypatch.setenv("CLICKHOUSE_PASSWORD", "env-pw")  # the second of the field's alias choices

    config = AuditConfig(_env_file=project)

    assert config.openai_api_key == "user-key"
    assert config.s2_api_key == "project-s2"
    assert config.model == "env-model"
    assert config.clickhouse_password == "env-pw"


def test_openalex_api_key_reaches_the_openalex_adapter(user_config):
    from reference_audit.sources.registry import build_default_adapters

    user_config.write_text('openalex_api_key = "user-oa"\nsource_backend = "api"\n')
    adapters = {a.name: a for a in build_default_adapters(AuditConfig(_env_file=None))}
    assert adapters["openalex"].api_key == "user-oa"


def test_constructor_arguments_override_user_config(user_config):
    user_config.write_text('model = "user-model"\n')
    assert AuditConfig(_env_file=None, model="arg-model").model == "arg-model"


def test_missing_user_config_is_skipped(user_config):
    assert AuditConfig(_env_file=None).openai_api_key is None


def test_unknown_key_is_an_error_naming_it(user_config):
    user_config.write_text('OPENAI_API_KEY = "k"\nopenai_api_kye = "k"\n')
    with pytest.raises(UserConfigError, match="OPENAI_API_KEY, openai_api_kye"):
        AuditConfig(_env_file=None)


def test_malformed_toml_is_an_error_naming_the_file(user_config):
    user_config.write_text("OPENAI_API_KEY=sk-not-quoted\n")
    with pytest.raises(UserConfigError, match="config.toml"):
        AuditConfig(_env_file=None)


def test_cli_reports_a_bad_user_config_and_exits_2(user_config, tmp_path):
    from typer.testing import CliRunner

    from reference_audit import cli

    user_config.write_text('openai_api_kye = "k"\n')
    bib = tmp_path / "refs.bib"
    bib.write_text("@article{k,title={T}}", encoding="utf-8")
    tex = tmp_path / "paper.tex"
    tex.write_text("\\cite{k}", encoding="utf-8")
    result = CliRunner().invoke(cli.app, ["audit", str(tex), str(bib)])
    assert result.exit_code == 2
    assert "unknown setting(s)" in result.output and "openai_api_kye" in result.output
