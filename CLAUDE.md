# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Yinshi is a web app that mimics core features of . Users can import GitHub or local git repositories as "workspaces," spawn git worktrees with random branch names, and interact with a `pi` coding agent (https://github.com/badlogic/pi-mono/tree/main). All data (conversations, plans, pi configs, worktrees) is stored in SQLite.

## Tech Stack

- **Backend**: Python (FastAPI), SQLite
- **Frontend**: Next.js
- **Agent**: pi coding agent integration

## Architecture

- Python conventions and patterns are defined in `PYTHON.md` -- follow it for all backend code
- Follow the `src/` layout from `PYTHON.md` for the Python backend
- Use pydantic for data validation and settings management
- Use async/await for I/O-bound operations
- Use parameterized queries for all SQLite access

## Development

```bash
# Python backend
python -m venv venv && source venv/bin/activate
pip install -r requirements/dev.txt
pytest --cov=src
black src tests && isort src tests && flake8 src tests && mypy src

# Run single test
pytest tests/path/to/test_file.py::test_name -v

# Frontend
npm install
npm run dev
```

## Design Principles

- "Don't make me think" -- consistent, simple UX
- No frills, just performance
- No plan mode, commits, or file change history -- focus on agent interaction only

## Key Files

- `GOAL.md` -- full project requirements and vision
- `PYTHON.md` -- Python coding standards (error handling, type hints, async, testing, logging)
