"""Runner restart preserves a committed terminal event before interrupting orphans."""

from tests.test_prompt_journal import _seed_session
from yinshi.runner_worker import RunnerWorkerManager
from yinshi.tenant import get_user_db


def test_worker_activation_preserves_a_committed_result_event(db, tmp_path, monkeypatch):
    options = {
        "data_directory": tmp_path / "runner",
        "data_protection_key": b"r" * 32,
        "environment_setter": monkeypatch.setenv,
    }
    first = RunnerWorkerManager(**options).dispatcher("account-1")
    run_id = "d" * 32
    with get_user_db(first.tenant) as database:
        session_id = _seed_session(database)
        database.execute(
            "INSERT INTO prompt_runs (id, session_id, idempotency_key, status) VALUES (?, ?, 'initial', 'running')",
            (run_id, session_id),
        )
        database.execute(
            'INSERT INTO prompt_events (run_id, sequence, event_json) VALUES (?, 0, \'{"type":"result"}\')',
            (run_id,),
        )
        database.commit()
    restarted = RunnerWorkerManager(**options).dispatcher("account-1")
    with get_user_db(restarted.tenant) as database:
        assert (
            database.execute("SELECT status FROM prompt_runs WHERE id = ?", (run_id,)).fetchone()[0]
            == "completed"
        )
        events = database.execute(
            "SELECT event_json FROM prompt_events WHERE run_id = ? ORDER BY sequence", (run_id,)
        ).fetchall()
        assert [row[0] for row in events] == ['{"type":"result"}']
