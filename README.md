# dspace-mcp-write

[![CI](https://github.com/mpasternak/dspace-mcp-write/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mpasternak/dspace-mcp-write/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/dspace-mcp-write.svg)](https://pypi.org/project/dspace-mcp-write/)
[![Python](https://img.shields.io/pypi/pyversions/dspace-mcp-write.svg)](https://pypi.org/project/dspace-mcp-write/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

[![Install in Claude Desktop](https://img.shields.io/badge/Install_in-Claude_Desktop-D97757?style=for-the-badge&logo=anthropic&logoColor=white)](https://github.com/mpasternak/dspace-mcp-write/releases/latest/download/dspace-mcp-write.mcpb)
[![Install in Cursor](https://img.shields.io/badge/Install_in-Cursor-000000?style=for-the-badge&logo=cursor&logoColor=white)](https://cursor.com/en/install-mcp?name=dspace-mcp-write&config=eyJjb21tYW5kIjoidXZ4IiwiYXJncyI6WyJkc3BhY2UtbWNwLXdyaXRlIl0sImVudiI6eyJEU1BBQ0VfQkFTRV9VUkwiOiIiLCJEU1BBQ0VfVVNFUk5BTUUiOiIiLCJEU1BBQ0VfUEFTU1dPUkQiOiIifX0=)
[![Install in VS Code](https://img.shields.io/badge/Install_in-VS_Code-0098FF?style=for-the-badge&logo=visualstudiocode&logoColor=white)](https://vscode.dev/redirect?url=vscode:mcp/install?%7B%22name%22%3A%22dspace-mcp-write%22%2C%22command%22%3A%22uvx%22%2C%22args%22%3A%5B%22dspace-mcp-write%22%5D%2C%22env%22%3A%7B%22DSPACE_BASE_URL%22%3A%22%22%2C%22DSPACE_USERNAME%22%3A%22%22%2C%22DSPACE_PASSWORD%22%3A%22%22%7D%7D)

> **This server changes your repository.** One click installs it, but it acts
> with the rights of the technical account you give it. The installer asks for
> the base URL, an EPerson e-mail and a password; the Cursor and VS Code links
> carry those keys empty, to be filled in locally rather than pasted into a URL.
> Set `DSPACE_WRITE_COLLECTIONS` to an allow-list of collection UUIDs unless you
> really mean "may write anywhere this account can reach".

An authenticated, **write-capable** [MCP](https://modelcontextprotocol.io/)
server for [DSpace](https://dspace.org/) 7+ repositories. It lets an AI
assistant **deposit new items, upload files, edit metadata, and create
collections** — on top of everything the read-only
[`dspace-mcp`](https://github.com/mpasternak/dspace-mcp) server already does
(search, fetch, browse, extract PDF text). All of `dspace-mcp`'s read tools are
included here, now authenticated.

> **This is the sharp tool.** If you only need to *read* a repository, run
> [`dspace-mcp`](https://github.com/mpasternak/dspace-mcp) instead — it holds no
> credentials and cannot change anything. Run `dspace-mcp-write` only when you
> actually want an assistant that can modify your repository, and read the
> Security section below first.

## Security — read this before you run it

Unlike the read-only server, `dspace-mcp-write` **holds credentials** for a real
DSpace account and **can modify your repository**. Be deliberate about it:

- **`confirm` is a usability guardrail, not a security boundary.** Every
  mutating tool defaults to `confirm=False`, returning a *preview* of what it
  would do without doing it. That helps a well-behaved assistant show you a plan
  before acting — but it is set by the same model that calls the tool, and this
  server also ingests untrusted repository content (PDF text), a classic
  prompt-injection vector. Do not treat `confirm` as a wall.
- **The real trust boundary is DSpace itself:** the permissions of the technical
  account you configure, plus your MCP host's approval of each tool call.
- **Recommended setup:** use a **narrowly-permissioned** technical account (submit
  rights only on the collections you intend, not an admin), and set
  **`DSPACE_WRITE_COLLECTIONS`** to the specific collections you want writable.
  With that, the blast radius is bounded by DSpace, not by the model's goodwill.

Deposits are **draft-first**: creating an item and uploading a file leave it as a
*workspace* draft. Nothing becomes public until the separate, explicit
`deposit_workspace_item` tool submits it to the collection's workflow.

## Install

With [uv](https://docs.astral.sh/uv/):

```bash
uvx dspace-mcp-write --base-url https://demo.dspace.org/server \
  --username you@example.org --password '••••••'
```

Or with pip:

```bash
pip install dspace-mcp-write
```

Credentials are **required** — running this server *is* the opt-in to writing.
Prefer environment variables over command-line flags so secrets don't land in
your shell history or process list (see Configuration).

## Configure your MCP client

**Claude Code:**

```bash
claude mcp add dspace-write \
  --env DSPACE_BASE_URL=https://demo.dspace.org/server \
  --env DSPACE_USERNAME=you@example.org \
  --env DSPACE_PASSWORD=secret \
  --env DSPACE_WRITE_COLLECTIONS=<collection-uuid>,<collection-uuid> \
  -- uvx dspace-mcp-write
```

**Claude Desktop / any client using `mcp.json`:**

```json
{
  "mcpServers": {
    "dspace-write": {
      "command": "uvx",
      "args": ["dspace-mcp-write"],
      "env": {
        "DSPACE_BASE_URL": "https://demo.dspace.org/server",
        "DSPACE_USERNAME": "you@example.org",
        "DSPACE_PASSWORD": "secret",
        "DSPACE_WRITE_COLLECTIONS": "<collection-uuid>"
      }
    }
  }
}
```

## Tools

Every mutating tool takes a `confirm` flag: call it with `confirm=false` (the
default) to get a preview, `confirm=true` to actually perform the change.

### Write tools

| Tool | What it does |
|---|---|
| `get_submission_form` | Show a collection's submission fields and which are required — check this *before* depositing. |
| `create_workspace_item` | Start a draft in a collection and fill its metadata (leaves it as a draft). |
| `update_workspace_item_metadata` | Fix or complete a draft's metadata after a validation error. |
| `upload_file_to_workspace_item` | Attach a file to a draft. |
| `deposit_workspace_item` | Submit a draft to the collection's workflow — the **only** tool that publishes. Requires `grant_license=true` when the collection needs the deposit licence accepted. |
| `discard_workspace_item` | Delete a draft you created. |
| `add_file_to_item` | Attach a file to an existing, archived item. |
| `update_item_metadata` | Edit an existing item's metadata. |
| `create_collection` / `create_community` | Create repository structure. |

### Read tools

All nine tools from [`dspace-mcp`](https://github.com/mpasternak/dspace-mcp) are
registered here too (`search_items`, `get_item`, `list_communities`,
`list_collections`, `list_bitstreams`, `get_bitstream_text`,
`list_facet_values`, `get_item_statistics`, `get_repository_info`) — now
authenticated, so they can also see non-public content the account is allowed to
read.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DSPACE_BASE_URL` | *(required)* | REST API root, e.g. `https://demo.dspace.org/server` |
| `DSPACE_USERNAME` | *(required)* | technical account e-mail |
| `DSPACE_PASSWORD` | *(required)* | account password |
| `DSPACE_WRITE_COLLECTIONS` | *(empty = all)* | comma-separated collection UUIDs the server may deposit into / attach files to |
| `DSPACE_UPLOAD_MAX_MB` | `100` | refuse to upload files larger than this |
| `DSPACE_TIMEOUT` | `15` | seconds per HTTP request |
| `DSPACE_MAX_RESULTS` | `50` | ceiling on records the read tools return |

Every variable has a matching flag (`--base-url`, `--username`, `--password`,
`--write-collections`, `--upload-max-mb`, …). `DSPACE_WRITE_COLLECTIONS` bounds
deposits and file uploads; it does **not** restrict `create_collection` /
`create_community`, which have no target collection — rely on the account's
permissions for those.

## How it talks to DSpace

DSpace 7+ writes go over an authenticated REST surface: a CSRF handshake
(`GET /api/security/csrf`), a JWT login (`POST /api/authn/login`), and a
submission flow (`/api/submission/workspaceitems` → JSON-Patch metadata →
multipart file upload → `/api/workflow/workflowitems`). This server manages the
rotating CSRF token and the JWT for you, scopes those credentials strictly to
your DSpace host (never leaking them to redirect targets such as S3/CDN), and
discovers each collection's submission form at runtime rather than assuming a
fixed field layout. The design and its rationale — including the API quirks
verified against a live instance — live in `docs/superpowers/specs/`.

## Development

```bash
git clone https://github.com/mpasternak/dspace-mcp-write
cd dspace-mcp-write
uv sync --dev
uv run pytest              # unit tests, offline (respx)
uv run ruff check .

# Live contract tests hit a real DSpace and need a real account.
# demo.dspace.org has a public demo admin account:
export DSPACE_TEST_USER='dspacedemo+admin@gmail.com'
export DSPACE_TEST_PASSWORD='dspace'
uv run pytest -m live
```

The live round-trip creates a draft, uploads a small file, verifies it, and
discards the draft — it cleans up after itself. `demo.dspace.org` is a shared,
frequently-reset instance, so the live job is best-effort, not a release gate.

## License

MIT — see [LICENSE](LICENSE).
