"""Keep desktop recovery scoped to the selected shared database."""

import asyncio
import uuid

from tests.test_prompt_journal import _request, _seed_session
from yinshi.services.prompt_journal import PromptJournal
from yinshi.tenant import TenantContext


async def test_desktop_reconnect_ignores_unused_tenant_database_paths(db, tmp_path):
    session_id = _seed_session(db)
    first_request = _request()
    first_request.app.state.mode = "desktop"
    second_request = _request()
    second_request.scope["app"] = first_request.app
    for request, name in ((first_request, "first"), (second_request, "second")):
        request.state.tenant = TenantContext(
            user_id="same-user",
            email="same@example.com",
            data_dir=str(tmp_path),
            db_path=str(tmp_path / f"unused-{name}.db"),
        )
    started = asyncio.Event()

    async def executor(request, session_id, body):
        started.set()
        await asyncio.Event().wait()
        yield {"type": "result"}

    journal = PromptJournal(executor=executor)
    try:
        run = await journal.start(
            request=first_request,
            session_id=session_id,
            idempotency_key=str(uuid.uuid4()),
            body={"prompt": "Remain active during reconnect"},
        )
        await asyncio.wait_for(started.wait(), timeout=2)
        batch = await journal.events(
            request=second_request,
            session_id=session_id,
            run_id=run.id,
            next_sequence=0,
        )
        assert batch.status == "running"
    finally:
        await journal.close()
