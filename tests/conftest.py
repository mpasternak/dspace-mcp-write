"""Współdzielona konfiguracja testów.

Ten plik jest wspólny dla wszystkich plików testowych — pojedyncze testy
dokładają własne fixture'y lokalnie, tego pliku nie modyfikują.
"""

from __future__ import annotations

import pytest

from dspace_mcp_write.config import WriteConfig

#: Bazowy URL używany w testach jednostkowych (respx). Host inny niż S3/CDN,
#: dzięki czemu testy origin-scopingu mają wyraźnie „obcy" host do sprawdzenia.
BASE_URL = "https://dspace.example.org/server"
API = f"{BASE_URL}/api"


def make_config(**overrides: object) -> WriteConfig:
    """Zbuduj ``WriteConfig`` z rozsądnymi domyślnymi, nadpisywalnymi w teście."""
    params: dict[str, object] = {
        "base_url": BASE_URL,
        "username": "tech@example.org",
        "password": "secret",
        "write_collections": (),
        "upload_max_mb": 100,
    }
    params.update(overrides)
    return WriteConfig(**params)  # type: ignore[arg-type]


@pytest.fixture
def write_config() -> WriteConfig:
    """Domyślna konfiguracja zapisu dla testów."""
    return make_config()
