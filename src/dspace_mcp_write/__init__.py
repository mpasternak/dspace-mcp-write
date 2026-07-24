"""dspace-mcp-write — uwierzytelniony serwer MCP z zapisem dla DSpace 7+.

Nadzbiór read-only serwera ``dspace-mcp``: rejestruje wszystkie jego narzędzia
odczytu plus warstwę zapisu (deponowanie, wgrywanie plików, edycja metadanych,
struktura). Szczegóły projektu: ``docs/superpowers/specs/``.
"""

from __future__ import annotations

__version__ = "0.1.0"
