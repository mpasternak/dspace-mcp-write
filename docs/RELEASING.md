# Releasing dspace-mcp-write

Publishing to PyPI uses **Trusted Publishing (OIDC)** — no API token is stored
anywhere. PyPI trusts the `publish.yml` workflow running in this repository,
under the `pypi` environment. The workflow is already in
`.github/workflows/publish.yml`.

Two one-time setup steps are needed before the first release.

## 1. One-time: register the Trusted Publisher on PyPI

Because `dspace-mcp-write` does not exist on PyPI yet, register a **pending
publisher** (this lets the very first upload create the project via OIDC, with
no token):

1. Sign in at <https://pypi.org>.
2. Go to **Account settings → Publishing** (<https://pypi.org/manage/account/publishing/>).
3. Under **Add a new pending publisher → GitHub**, fill in exactly:
   - **PyPI Project Name:** `dspace-mcp-write`
   - **Owner:** `mpasternak`
   - **Repository name:** `dspace-mcp-write`
   - **Workflow name:** `publish.yml`
   - **Environment name:** `pypi`
4. Save.

(After the first successful release the project exists; the pending publisher
becomes a normal Trusted Publisher automatically — nothing else to do.)

## 2. One-time: create the `pypi` environment on GitHub

1. Repo **Settings → Environments → New environment**, name it exactly `pypi`.
2. (Optional but recommended) add yourself as a **required reviewer** so every
   release waits for a manual click before it publishes. This is why the
   workflow declares `environment: name: pypi`.

## 3. Cutting a release

1. Bump the version in `pyproject.toml` (and `src/dspace_mcp_write/__init__.py`,
   which feeds the `User-Agent`) — keep them in sync.
2. Commit and push to `main`; make sure CI is green.
3. Create a GitHub **Release** with a tag like `v0.1.0`
   (`gh release create v0.1.0 --generate-notes`).
4. The `Publish to PyPI` workflow triggers on the published release: it runs the
   tests, builds the sdist + wheel, `twine check`s them, and publishes via OIDC.
   If you set a required reviewer, approve the run in the Actions tab.

That's it — no PyPI token, no secrets in the repo.

## Notes

- Third-party GitHub Actions are pinned to commit SHAs (supply-chain safety);
  when you bump them, update the SHA and the `# vX.Y.Z` comment together.
- The workflow also runs on `workflow_dispatch`, so you can trigger a manual
  (re)publish from the Actions tab if a release run needs to be re-run.
