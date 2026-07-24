"""Tests for the write server's configuration layer."""

from __future__ import annotations

import dataclasses

import pytest

from conftest import make_config
from dspace_mcp_write.config import (
    DEFAULT_UPLOAD_MAX_MB,
    config_from_env,
    parse_args,
)

_CONFIG_ENV_VARS = (
    "DSPACE_BASE_URL",
    "DSPACE_USERNAME",
    "DSPACE_PASSWORD",
    "DSPACE_WRITE_COLLECTIONS",
    "DSPACE_UPLOAD_MAX_MB",
    "DSPACE_TIMEOUT",
    "DSPACE_MAX_RESULTS",
)


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "DSPACE_BASE_URL": "https://dspace.example.org/server",
        "DSPACE_USERNAME": "tech@example.org",
        "DSPACE_PASSWORD": "secret",
    }
    env.update(overrides)
    return env


# --- config_from_env: happy path ------------------------------------------


def test_config_from_env_minimal() -> None:
    cfg = config_from_env(_base_env())
    assert cfg.base_url == "https://dspace.example.org/server"
    assert cfg.username == "tech@example.org"
    assert cfg.password == "secret"
    assert cfg.write_collections == ()
    assert cfg.upload_max_mb == DEFAULT_UPLOAD_MAX_MB


def test_config_from_env_normalizes_base_url() -> None:
    # normalize_base_url strips a trailing /api and trailing slashes.
    cfg = config_from_env(
        _base_env(DSPACE_BASE_URL="https://dspace.example.org/server/api/")
    )
    assert cfg.base_url == "https://dspace.example.org/server"


# --- config_from_env: required credentials --------------------------------


def test_missing_base_url_raises() -> None:
    env = _base_env()
    del env["DSPACE_BASE_URL"]
    with pytest.raises(ValueError, match="DSPACE_BASE_URL"):
        config_from_env(env)


def test_missing_username_raises() -> None:
    env = _base_env()
    del env["DSPACE_USERNAME"]
    with pytest.raises(ValueError, match="DSPACE_USERNAME"):
        config_from_env(env)


def test_missing_password_raises() -> None:
    env = _base_env()
    del env["DSPACE_PASSWORD"]
    with pytest.raises(ValueError, match="DSPACE_PASSWORD"):
        config_from_env(env)


def test_blank_username_raises() -> None:
    with pytest.raises(ValueError, match="DSPACE_USERNAME"):
        config_from_env(_base_env(DSPACE_USERNAME="   "))


def test_blank_password_raises() -> None:
    with pytest.raises(ValueError, match="DSPACE_PASSWORD"):
        config_from_env(_base_env(DSPACE_PASSWORD=""))


# --- whitelist CSV parsing -------------------------------------------------


def test_whitelist_csv_parsing() -> None:
    cfg = config_from_env(_base_env(DSPACE_WRITE_COLLECTIONS="uuid-1, uuid-2 ,uuid-3"))
    assert cfg.write_collections == ("uuid-1", "uuid-2", "uuid-3")


def test_whitelist_empty_means_all() -> None:
    cfg = config_from_env(_base_env(DSPACE_WRITE_COLLECTIONS=""))
    assert cfg.write_collections == ()


def test_whitelist_skips_blank_entries() -> None:
    cfg = config_from_env(_base_env(DSPACE_WRITE_COLLECTIONS="uuid-1,,  , uuid-2"))
    assert cfg.write_collections == ("uuid-1", "uuid-2")


# --- upload_max_mb parsing -------------------------------------------------


def test_upload_max_mb_parsing() -> None:
    cfg = config_from_env(_base_env(DSPACE_UPLOAD_MAX_MB="250"))
    assert cfg.upload_max_mb == 250
    assert cfg.upload_max_bytes == 250 * 1024 * 1024


def test_upload_max_mb_invalid_raises() -> None:
    with pytest.raises(ValueError, match="DSPACE_UPLOAD_MAX_MB"):
        config_from_env(_base_env(DSPACE_UPLOAD_MAX_MB="not-a-number"))


def test_upload_max_mb_non_positive_raises() -> None:
    with pytest.raises(ValueError, match="DSPACE_UPLOAD_MAX_MB"):
        config_from_env(_base_env(DSPACE_UPLOAD_MAX_MB="0"))


# --- frozen dataclass ------------------------------------------------------


def test_writeconfig_is_frozen() -> None:
    cfg = make_config()
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.upload_max_mb = 5  # type: ignore[misc]


def test_writeconfig_enable_write_is_ignored_default() -> None:
    # enable_write is inherited but plays no role here; it defaults to False.
    cfg = make_config()
    assert cfg.enable_write is False


# --- collection_allowed / upload_max_bytes ---------------------------------


def test_collection_allowed_empty_allows_all() -> None:
    cfg = make_config(write_collections=())
    assert cfg.collection_allowed("any-uuid") is True


def test_collection_allowed_respects_whitelist() -> None:
    cfg = make_config(write_collections=("uuid-1", "uuid-2"))
    assert cfg.collection_allowed("uuid-1") is True
    assert cfg.collection_allowed("uuid-2") is True
    assert cfg.collection_allowed("uuid-3") is False


def test_upload_max_bytes_property() -> None:
    cfg = make_config(upload_max_mb=100)
    assert cfg.upload_max_bytes == 100 * 1024 * 1024


# --- parse_args ------------------------------------------------------------


def test_parse_args_from_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    cfg = parse_args(
        [
            "--base-url",
            "https://dspace.example.org/server",
            "--username",
            "tech@example.org",
            "--password",
            "secret",
            "--write-collections",
            "uuid-a,uuid-b",
            "--upload-max-mb",
            "42",
        ]
    )
    assert cfg.base_url == "https://dspace.example.org/server"
    assert cfg.username == "tech@example.org"
    assert cfg.password == "secret"
    assert cfg.write_collections == ("uuid-a", "uuid-b")
    assert cfg.upload_max_mb == 42


def test_parse_args_defaults_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("DSPACE_BASE_URL", "https://dspace.example.org/server")
    monkeypatch.setenv("DSPACE_USERNAME", "tech@example.org")
    monkeypatch.setenv("DSPACE_PASSWORD", "secret")
    cfg = parse_args([])
    assert cfg.username == "tech@example.org"
    assert cfg.upload_max_mb == DEFAULT_UPLOAD_MAX_MB


def test_parse_args_missing_password_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="DSPACE_PASSWORD"):
        parse_args(
            [
                "--base-url",
                "https://dspace.example.org/server",
                "--username",
                "tech@example.org",
            ]
        )


def test_parse_args_missing_base_url_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="DSPACE_BASE_URL"):
        parse_args(["--username", "tech@example.org", "--password", "secret"])


def test_parse_args_non_positive_upload_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in _CONFIG_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ValueError, match="upload-max-mb"):
        parse_args(
            [
                "--base-url",
                "https://dspace.example.org/server",
                "--username",
                "tech@example.org",
                "--password",
                "secret",
                "--upload-max-mb",
                "-3",
            ]
        )
