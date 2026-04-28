# Yinshi

Yinshi is a browser-based coding environment for running [pi](https://pi.dev) agents against Git repositories. Users import a GitHub repository or an allowed local repository, create an isolated workspace branch, chat with the agent, then review the resulting branch through normal Git tooling.

## How It Works

Importing a repository stores a bare checkout on the server. Private GitHub repositories use the GitHub App integration. A workspace is a git worktree on a throwaway branch, so main stays untouched until the user merges outside Yinshi. The chat view streams pi agent events while the agent edits files inside that workspace.

Yinshi is designed for browser-first use. It removes local IDE setup, works from mobile browsers, and lets users bring their own model-provider credentials. Provider secrets are stored through AES-256-GCM encryption.

## Data Protection Model

Yinshi uses a middle-ground security model rather than zero-knowledge hosting. The server still sees plaintext while it runs user sessions. Stored user data is protected through per-user encryption keys, optional SQLCipher tenant databases, encrypted sensitive control fields, narrow sidecar container mounts, and HTTPS enforcement. See [docs/security/middle-ground-threat-model.md](docs/security/middle-ground-threat-model.md) for the exact guarantee, the non-guarantee, and operator duties.

## Architecture

The backend is FastAPI. SQLite stores data in a control database plus one tenant database per user. Pydantic handles request validation and settings. Authlib provides Google and GitHub OAuth. The `cryptography` package handles AES-256-GCM and HKDF. `slowapi` rate-limits sensitive routes. `httpx` is used for outbound HTTP calls. Uvicorn serves the ASGI app.

The frontend is React 18 with React Router, TypeScript, Tailwind CSS, Vite, Vitest, and Playwright. Chat markdown rendering uses `react-markdown` with `remark-gfm`.

The sidecar is a Node.js bridge to the pi coding agent SDK. In tenant mode, the backend starts a dedicated Podman container per user and talks to the sidecar over a Unix domain socket. By default the sidecar receives only the active runtime paths it needs, not the user's whole data directory.

SQLCipher support is optional at install time. Production deployments that set `TENANT_DB_ENCRYPTION=required` must install either `sqlcipher3` or `pysqlcipher3` in the backend environment.

## Development

Backend development uses per-user containers by default. Set `CONTAINER_ENABLED=false` only for explicit no-auth, development, or test execution on the host.

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements/base.txt
cp .env.example .env
uvicorn yinshi.main:app --reload
```

## Project Structure

```text
backend/
  src/yinshi/            FastAPI app, tenant DB code, auth, services, API routes
  tests/                 pytest suite
frontend/
  src/                   React app, API client, hooks, pages, components
  public/                static assets
sidecar/
  src/                   Node.js pi agent bridge
lit/
  yinshi.lit.md          annotated source document
```

## Documentation

The full annotated source is available as a literate program: [lit/yinshi.lit.md](lit/yinshi.lit.md).

## License

Use it or don't.
