"""Runtime activation respects authenticated storage boundaries."""

import httpx
import pytest

from tests.test_prompt_journal import _seed_session


def test_authenticated_request_recovers_only_selected_tenant_storage(auth_client):
    from yinshi.db import get_db
    from yinshi.tenant import get_user_db

    run_id = "e" * 32
    with get_db() as legacy:
        session_id = _seed_session(legacy)
        legacy.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, 'initial', 'running')",
            (run_id, session_id),
        )
        legacy.commit()
    with get_user_db(auth_client.yinshi_tenant) as tenant:
        tenant_session_id = _seed_session(tenant)
        tenant.execute("UPDATE sessions SET id = ? WHERE id = ?", (session_id, tenant_session_id))
        tenant.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, 'initial', 'running')",
            (run_id, session_id),
        )
        tenant.execute(
            "INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, 0, ?)",
            (run_id, '{"type":"result"}'),
        )
        tenant.commit()

    response = auth_client.get("/api/repos")
    assert response.status_code == 200
    with get_user_db(auth_client.yinshi_tenant) as tenant:
        assert (
            tenant.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
            == "completed"
        )
    with get_db() as legacy:
        assert (
            legacy.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
            == "running"
        )


async def test_control_only_request_leaves_legacy_execution_storage_unchanged(db, monkeypatch):
    import yinshi.main as main_module

    session_id, run_id = _seed_session(db), "f" * 32
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, 'initial', 'running')",
        (run_id, session_id),
    )
    db.commit()
    settings = main_module.get_settings().model_copy(
        update={"managed_runtime_provider": "fly_sprites"}
    )
    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    app = main_module.create_app(mode="hosted")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get("/api/repos")
        assert response.status_code == 404
        assert (
            db.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
            == "running"
        )
    finally:
        await app.state.prompt_journal.close()


@pytest.mark.parametrize("path", ["/health", "/api/repos"])
async def test_unauthenticated_requests_do_not_activate_fallback_execution_storage(db, path):
    from yinshi.main import create_app

    session_id, run_id = _seed_session(db), "d" * 32
    db.execute(
        "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, 'initial', 'running')",
        (run_id, session_id),
    )
    db.commit()
    app = create_app(mode="hosted")
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            response = await client.get(path)
        assert response.status_code == 200
        assert (
            db.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
            == "running"
        )
    finally:
        await app.state.prompt_journal.close()
