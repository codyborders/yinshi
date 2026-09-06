"""Verify additive Phase 5 storage on fresh and existing databases."""


def test_active_artifact_namespace_claim_is_unique_and_reusable_after_release(db, git_repo) -> None:
    import sqlite3

    import pytest

    from tests.test_thread_orchestration import seed_parent_stack

    columns = {row[1] for row in db.execute("PRAGMA table_info(thread_delegations)")}
    assert "git_artifact_namespace" in columns
    seed_parent_stack(db, git_repo)
    namespace = "a" * 64
    statement = (
        "INSERT INTO thread_delegations "
        "(id, parent_session_id, idempotency_key, initiator, title, task, requested_model, status, git_artifacts_claimed, git_artifact_namespace) "
        "VALUES (?, 'parent-session', ?, 'user', 'Child', 'Inspect', 'model', 'provisioning', 1, ?)"
    )
    db.execute(statement, ("1" * 32, "first", namespace))
    db.commit()
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(statement, ("2" * 32, "second", namespace))
    db.rollback()
    db.execute("UPDATE thread_delegations SET git_artifacts_claimed = 0 WHERE id = ?", ("1" * 32,))
    db.commit()
    db.execute(statement, ("2" * 32, "second", namespace))
    db.commit()
    assert (
        db.execute(
            "SELECT COUNT(*) FROM thread_delegations WHERE git_artifact_namespace = ? AND git_artifacts_claimed = 1",
            (namespace,),
        ).fetchone()[0]
        == 1
    )


def test_tenant_upgrade_adds_missing_artifact_claim_column(db) -> None:
    from yinshi.tenant import _ensure_current_user_db_schema

    db.execute("DROP INDEX thread_delegations_git_namespace_claim")
    db.execute("ALTER TABLE thread_delegations DROP COLUMN git_artifact_namespace")
    db.execute("ALTER TABLE thread_delegations DROP COLUMN git_artifacts_claimed")
    db.execute("PRAGMA user_version = 2")
    db.commit()
    _ensure_current_user_db_schema(db)
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(thread_delegations)")}
    assert "git_artifacts_claimed" in columns
    assert columns["git_artifacts_claimed"][3] == 1
    assert columns["git_artifacts_claimed"][4] == "0"


def test_git_artifact_claim_defaults_to_unowned(db) -> None:
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(thread_delegations)")}
    assert "git_artifacts_claimed" in columns
    assert columns["git_artifacts_claimed"][3] == 1
    assert columns["git_artifacts_claimed"][4] == "0"


def test_delegations_default_to_explicit_manual_queue(db) -> None:
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(thread_delegations)")}
    assert columns["auto_start"][4] == "0"


def test_tenant_upgrade_preserves_legacy_automatic_start_policy(db) -> None:
    from yinshi.tenant import _ensure_current_user_db_schema

    db.execute("ALTER TABLE thread_delegations DROP COLUMN auto_start")
    db.execute("PRAGMA user_version = 2")
    db.commit()
    _ensure_current_user_db_schema(db)
    columns = {row[1]: row for row in db.execute("PRAGMA table_info(thread_delegations)")}
    assert columns["auto_start"][4] == "0"
    assert db.execute("PRAGMA user_version").fetchone()[0] == 3
