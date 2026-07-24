"""Configuration for the write server: ``WriteConfig`` plus env/CLI loaders.

This is an authenticated superset of ``dspace-mcp``'s read-only configuration.
We reuse the parent :class:`~dspace_mcp.config.Config` dataclass and
:func:`~dspace_mcp.config.normalize_base_url`, but reimplement the env/CLI layer
because ``dspace-mcp``'s own loaders hardcode the ``Config`` type, never read the
credentials, and expose a private parser named ``dspace-mcp``.

Credentials (``username``/``password``) are inherited from ``Config`` and are
*required* here: without them the server cannot log in, so both loaders raise a
clear English ``ValueError`` when they are missing.

All user-facing strings are in English - this is an international package.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TypeVar

from dspace_mcp.config import Config, normalize_base_url

ENV_BASE_URL = "DSPACE_BASE_URL"
ENV_USERNAME = "DSPACE_USERNAME"
ENV_PASSWORD = "DSPACE_PASSWORD"
ENV_WRITE_COLLECTIONS = "DSPACE_WRITE_COLLECTIONS"
ENV_UPLOAD_MAX_MB = "DSPACE_UPLOAD_MAX_MB"
ENV_TIMEOUT = "DSPACE_TIMEOUT"
ENV_MAX_RESULTS = "DSPACE_MAX_RESULTS"

# Kept in step with dspace_mcp.config; duplicated because the contract permits
# reusing only ``Config`` and ``normalize_base_url`` from that module.
DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RESULTS = 50
DEFAULT_UPLOAD_MAX_MB = 100

EXAMPLE_BASE_URL = "https://demo.dspace.org/server"

_N = TypeVar("_N", int, float)


@dataclass(frozen=True)
class WriteConfig(Config):
    """Complete settings for the write server.

    ``Config`` is a frozen dataclass, so this subclass MUST be frozen too - a
    non-frozen dataclass inheriting from a frozen one raises ``TypeError`` at
    class-creation time.

    ``username``/``password`` are inherited from ``Config`` (``str | None``);
    the factories below guarantee they are present. The inherited
    ``enable_write`` flag is deliberately ignored - on this server the presence
    of credentials is the effective opt-in, not that flag.
    """

    write_collections: tuple[str, ...] = ()
    upload_max_mb: int = DEFAULT_UPLOAD_MAX_MB

    @property
    def upload_max_bytes(self) -> int:
        """Upload size limit in bytes (we compare byte counts, not megabytes)."""
        return self.upload_max_mb * 1024 * 1024

    def collection_allowed(self, uuid: str) -> bool:
        """Is writing to ``uuid`` allowed?

        An empty whitelist means "all collections are allowed" (the default);
        otherwise only UUIDs explicitly listed may be written to.
        """
        return not self.write_collections or uuid in self.write_collections


def _missing_base_url_message() -> str:
    return (
        f"No DSpace base URL configured. Set ${ENV_BASE_URL} or pass --base-url, "
        f"e.g. {EXAMPLE_BASE_URL}."
    )


def _missing_credential_message(var: str, flag: str, what: str) -> str:
    return (
        f"No DSpace {what} configured. This write server requires credentials: "
        f"set ${var} or pass {flag} with the technical account's {what}."
    )


def _parse_collections(raw: str | None) -> tuple[str, ...]:
    """Parse a CSV of collection UUIDs; empty/unset means "all collections"."""
    if raw is None:
        return ()
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _coerce_positive(
    raw: str,
    *,
    label: str,
    converter: Callable[[str], _N],
    kind: str,
) -> _N:
    """Turn a string into a positive number, or say exactly what was wrong."""
    try:
        value = converter(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Invalid value for {label}: {raw!r} is not a valid {kind}."
        ) from exc
    if value <= 0:
        raise ValueError(f"Invalid value for {label}: {value} must be greater than 0.")
    return value


def _positive_from_env(
    env: Mapping[str, str],
    var: str,
    *,
    converter: Callable[[str], _N],
    kind: str,
    default: _N,
) -> _N:
    # A set-but-empty variable is almost always a typo in the MCP client's
    # config, so we surface it rather than silently using the default.
    raw = env.get(var)
    if raw is None:
        return default
    return _coerce_positive(raw, label=var, converter=converter, kind=kind)


def config_from_env(env: Mapping[str, str] | None = None) -> WriteConfig:
    """Build a :class:`WriteConfig` from environment variables.

    Raises ``ValueError`` (English message) if the base URL, username, or
    password is missing - all three are required for the server to start.
    """
    if env is None:
        env = os.environ

    raw_base_url = env.get(ENV_BASE_URL)
    if raw_base_url is None or not raw_base_url.strip():
        raise ValueError(_missing_base_url_message())

    username = env.get(ENV_USERNAME)
    if username is None or not username.strip():
        raise ValueError(
            _missing_credential_message(ENV_USERNAME, "--username", "username")
        )

    password = env.get(ENV_PASSWORD)
    if password is None or not password.strip():
        raise ValueError(
            _missing_credential_message(ENV_PASSWORD, "--password", "password")
        )

    return WriteConfig(
        base_url=normalize_base_url(raw_base_url),
        username=username.strip(),
        # Password is passed through verbatim: stripping a secret could corrupt
        # a legitimately whitespace-padded password.
        password=password,
        write_collections=_parse_collections(env.get(ENV_WRITE_COLLECTIONS)),
        upload_max_mb=_positive_from_env(
            env,
            ENV_UPLOAD_MAX_MB,
            converter=int,
            kind="integer",
            default=DEFAULT_UPLOAD_MAX_MB,
        ),
        timeout=_positive_from_env(
            env, ENV_TIMEOUT, converter=float, kind="number", default=DEFAULT_TIMEOUT
        ),
        max_results=_positive_from_env(
            env,
            ENV_MAX_RESULTS,
            converter=int,
            kind="integer",
            default=DEFAULT_MAX_RESULTS,
        ),
    )


def _build_parser() -> argparse.ArgumentParser:
    from dspace_mcp_write import __version__  # local import: avoid an import cycle

    parser = argparse.ArgumentParser(
        prog="dspace-mcp-write",
        description="Authenticated write MCP server for DSpace 7+ repositories.",
    )
    parser.add_argument(
        "--base-url",
        metavar="URL",
        default=None,
        help=(
            f"Base URL of the DSpace server, e.g. {EXAMPLE_BASE_URL}. "
            f"Defaults to ${ENV_BASE_URL}."
        ),
    )
    parser.add_argument(
        "--username",
        metavar="EMAIL",
        default=None,
        help=(
            "EPerson e-mail of the technical account used to authenticate. "
            f"Defaults to ${ENV_USERNAME}."
        ),
    )
    parser.add_argument(
        "--password",
        metavar="PASSWORD",
        default=None,
        help=f"Password for the technical account. Defaults to ${ENV_PASSWORD}.",
    )
    parser.add_argument(
        "--write-collections",
        metavar="UUIDS",
        default=None,
        help=(
            "Comma-separated collection UUIDs writes are restricted to. "
            f"Empty means all collections. Defaults to ${ENV_WRITE_COLLECTIONS}."
        ),
    )
    parser.add_argument(
        "--upload-max-mb",
        metavar="MB",
        type=int,
        default=None,
        help=(
            f"Reject uploads larger than this many megabytes "
            f"(default: {DEFAULT_UPLOAD_MAX_MB}, or ${ENV_UPLOAD_MAX_MB})."
        ),
    )
    parser.add_argument(
        "--timeout",
        metavar="SECONDS",
        type=float,
        default=None,
        help=(
            f"HTTP request timeout in seconds (default: {DEFAULT_TIMEOUT:g}, "
            f"or ${ENV_TIMEOUT}). Uploads use their own longer timeout."
        ),
    )
    parser.add_argument(
        "--max-results",
        metavar="N",
        type=int,
        default=None,
        help=(
            f"Hard cap on how many objects any read tool may return "
            f"(default: {DEFAULT_MAX_RESULTS}, or ${ENV_MAX_RESULTS})."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"dspace-mcp-write {__version__}"
    )
    return parser


def _resolve_number(
    cli_value: _N | None,
    *,
    label: str,
    env: Mapping[str, str],
    var: str,
    converter: Callable[[str], _N],
    kind: str,
    default: _N,
) -> _N:
    """A CLI flag wins over the environment, which wins over the default."""
    if cli_value is not None:
        if cli_value <= 0:
            raise ValueError(
                f"Invalid value for {label}: {cli_value} must be greater than 0."
            )
        return cli_value
    return _positive_from_env(env, var, converter=converter, kind=kind, default=default)


def parse_args(argv: list[str] | None = None) -> WriteConfig:
    """Build a :class:`WriteConfig` from CLI args, defaulting to the environment.

    Raises ``ValueError`` (English message) when the base URL, username, or
    password is missing, or when a numeric option is non-positive. Malformed
    numeric CLI syntax is still rejected by argparse itself.
    """
    env: Mapping[str, str] = os.environ
    parser = _build_parser()
    args = parser.parse_args(argv)

    raw_base_url = args.base_url if args.base_url is not None else env.get(ENV_BASE_URL)
    if raw_base_url is None or not raw_base_url.strip():
        raise ValueError(_missing_base_url_message())
    base_url = normalize_base_url(raw_base_url)

    username = args.username if args.username is not None else env.get(ENV_USERNAME)
    if username is None or not username.strip():
        raise ValueError(
            _missing_credential_message(ENV_USERNAME, "--username", "username")
        )

    password = args.password if args.password is not None else env.get(ENV_PASSWORD)
    if password is None or not password.strip():
        raise ValueError(
            _missing_credential_message(ENV_PASSWORD, "--password", "password")
        )

    raw_collections = (
        args.write_collections
        if args.write_collections is not None
        else env.get(ENV_WRITE_COLLECTIONS)
    )

    return WriteConfig(
        base_url=base_url,
        username=username.strip(),
        password=password,
        write_collections=_parse_collections(raw_collections),
        upload_max_mb=_resolve_number(
            args.upload_max_mb,
            label="--upload-max-mb",
            env=env,
            var=ENV_UPLOAD_MAX_MB,
            converter=int,
            kind="integer",
            default=DEFAULT_UPLOAD_MAX_MB,
        ),
        timeout=_resolve_number(
            args.timeout,
            label="--timeout",
            env=env,
            var=ENV_TIMEOUT,
            converter=float,
            kind="number",
            default=DEFAULT_TIMEOUT,
        ),
        max_results=_resolve_number(
            args.max_results,
            label="--max-results",
            env=env,
            var=ENV_MAX_RESULTS,
            converter=int,
            kind="integer",
            default=DEFAULT_MAX_RESULTS,
        ),
    )
