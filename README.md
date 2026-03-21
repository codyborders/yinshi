# Yinshi

Yinshi is a browser-based coding environment that uses [pi](https://pi.dev) agents.

## How It Works

1. **Import a repo** - Paste a GitHub URL, `user/repo` shorthand, or a local path. Yinshi clones it and stores the bare repo on the server. Private repos are supported through a GitHub App integration.

2. **Create a workspace** - Each workspace is a git worktree on a throwaway branch. Your main branch stays untouched.

3. **Chat with the agent** - Describe what you want built or changed. The pi agent reads, writes, and refactors code inside the workspace. Every change lives on the workspace branch.

4. **Review and merge** - When you're satisfied, merge the branch through standard git tooling. If you're not, discard it. Nothing touches main until you say so.

## What Makes It Useful

- **Zero local setup** - No IDE, no CLI, no environment configuration. Works from a phone, tablet, or any browser.
- **Isolation by default** - Every workspace runs on its own git branch. Experiments can't break production code.
- **Bring your own key** - Supply your own API keys for the underlying model providers. Keys are encrypted at rest with AES-256-GCM.
- **Multi-tenant** - Each user gets an isolated SQLite database. A control database manages authentication and shared state.
- **Mobile-first** - The interface adapts from phone to desktop. Start a coding session on your laptop, check progress from your phone.

## Tech Stack

### Backend

- [FastAPI](https://fastapi.tiangolo.com/) - async Python web framework
- [SQLite](https://sqlite.org/) - embedded database (per-user tenant DBs plus a control DB)
- [Pydantic](https://docs.pydantic.dev/) - data validation and settings management
- [Authlib](https://authlib.org/) - Google and GitHub OAuth authentication
- [cryptography](https://cryptography.io/) - AES-256-GCM encryption and HKDF key derivation
- [slowapi](https://github.com/laurentS/slowapi) - rate limiting on sensitive routes
- [httpx](https://www.python-httpx.org/) - async HTTP client
- [uvicorn](https://www.uvicorn.org/) - ASGI server

### Frontend

- [React 18](https://react.dev/) - UI framework
- [React Router](https://reactrouter.com/) - client-side routing
- [TypeScript](https://www.typescriptlang.org/) - type safety
- [Tailwind CSS](https://tailwindcss.com/) - utility-first styling with a custom parchment/ink color system
- [react-markdown](https://github.com/remarkjs/react-markdown) + [remark-gfm](https://github.com/remarkjs/remark-gfm) - markdown rendering in chat
- [Vite](https://vite.dev/) - build tooling
- [Vitest](https://vitest.dev/) + [Playwright](https://playwright.dev/) - unit and end-to-end testing

### Sidecar

- [pi coding agent SDK](https://pi.dev) - Node.js sidecar that bridges the backend to the pi agent over a Unix domain socket. Optionally runs inside a per-user Podman container for isolation.

## Development

```bash
# Backend
cd backend
python -m venv venv && source venv/bin/activate
pip install -r requirements/base.txt
cp .env.example .env  # configure secrets
uvicorn yinshi.main:app --reload

# Frontend
cd frontend
npm install
npm run dev

# Tests
cd backend && pytest
cd frontend && npx vitest run
```

## Project Structure

```
backend/
  src/yinshi/            # FastAPI application
    api/                 # Route handlers (auth, repos, workspaces, sessions, streaming, settings, github)
    services/            # Business logic (workspace lifecycle, git, crypto, container, pi config, keys)
    auth.py              # OAuth middleware and session management
    db.py                # SQLite schema, migrations, per-user tenant databases
    tenant.py            # Multi-tenant context resolution
    config.py            # Environment-based settings
    rate_limit.py        # Rate limiting configuration
    main.py              # App entry point
  tests/                 # pytest test suite

frontend/
  src/
    api/                 # API client
    components/          # Sidebar, ChatView, MessageBubble, Layout, PiConfigSection, etc.
    hooks/               # useAuth, useTheme, useAgentStream, usePiConfig
    pages/               # Landing, Session, Settings, EmptyState
  public/                # Static assets

sidecar/
  src/                   # Node.js pi agent bridge (Unix socket server)
```

## Documentation

The full annotated source is available as a literate program: [lit/yinshi.lit.md](lit/yinshi.lit.md).

## License

Use it or don't.
