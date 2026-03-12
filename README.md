# Yinshi

Browser-based coding with AI agents. Point Yinshi at a GitHub repo, and an AI agent writes code on an isolated branch while you review the diff. No local setup required -- just open a browser and go.

## How It Works

1. **Import a repo** -- Paste a GitHub URL, `user/repo` shorthand, or a local path. Yinshi clones it and stores the bare repo on the server.

2. **Create a workspace** -- Each workspace is a git worktree on a throwaway branch. Your main branch stays untouched.

3. **Chat with the agent** -- Describe what you want built or changed. The AI agent reads, writes, and refactors code inside the workspace. Every change lives on the workspace branch.

4. **Review and merge** -- When you're satisfied, merge the branch through standard git tooling. If you're not, discard it. Nothing touches main until you say so.

## What Makes It Useful

- **Zero local setup** -- No IDE, no CLI, no environment configuration. Works from a phone, tablet, or any browser.
- **Isolation by default** -- Every workspace runs on its own git branch. Experiments can't break production code.
- **Lightweight** -- SQLite for storage, a single Python backend, a static React frontend. No Kubernetes, no microservices.
- **Mobile-first** -- The interface adapts from phone to desktop. Start a coding session on your laptop, check progress from your phone.

## Tech Stack

### Backend

- [FastAPI](https://fastapi.tiangolo.com/) -- async Python web framework
- [SQLite](https://sqlite.org/) -- embedded database for repos, workspaces, sessions, and messages
- [Pydantic](https://docs.pydantic.dev/) -- data validation and settings management
- [Authlib](https://authlib.org/) -- Google OAuth authentication
- [uvicorn](https://www.uvicorn.org/) -- ASGI server

### Frontend

- [React 18](https://react.dev/) -- UI framework
- [React Router](https://reactrouter.com/) -- client-side routing
- [Tailwind CSS](https://tailwindcss.com/) -- utility-first styling with a custom parchment/ink color system
- [react-markdown](https://github.com/remarkjs/react-markdown) + [remark-gfm](https://github.com/remarkjs/remark-gfm) -- markdown rendering in chat and landing page
- [Vite](https://vite.dev/) -- build tooling
- [Vitest](https://vitest.dev/) -- test framework

### Agent

- [pi](https://github.com/badlogic/pi-mono/tree/main) -- coding agent that operates inside workspaces via a sidecar process

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
  src/yinshi/          # FastAPI application
    api/               # Route handlers (auth, repos, workspaces, sessions, streaming)
    services/          # Business logic (workspace lifecycle, git operations)
    auth.py            # OAuth middleware and session management
    db.py              # SQLite schema and connection
    main.py            # App entry point
  tests/               # pytest test suite

frontend/
  src/
    api/               # API client
    components/        # Sidebar, ChatView, MessageBubble, Layout, etc.
    hooks/             # useAuth, useTheme, useAgentStream
    pages/             # Landing, Session, EmptyState
  public/              # Static assets
```

## License

Private.
