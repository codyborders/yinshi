"""Supported migrations must reject incomplete Phase 5 persistence atomically."""

import sqlite3

import pytest

from yinshi import tenant


@pytest.mark.parametrize("encrypted", [False, True])
@pytest.mark.parametrize(
    "damage", ["cancel_column", "receipt_key", "index_unique", "index_column", "index_predicate"]
)
def test_incomplete_phase5_migration_rolls_back(tmp_path, monkeypatch, encrypted, damage):
    module = tenant._load_sqlcipher_module() if encrypted else sqlite3
    connection = module.connect(str(tmp_path / "migration.db"))
    try:
        if encrypted:
            connection.execute("PRAGMA key = 'test-migration-key'")
        tenant._ensure_current_user_db_schema(connection)
        connection.execute("PRAGMA user_version = 2")
        connection.commit()
        original_schema = connection.execute(
            "SELECT name, sql FROM sqlite_master ORDER BY name"
        ).fetchall()
        migrate = tenant._ensure_user_db_schema

        def incomplete(database):
            migrate(database)
            if damage == "cancel_column":
                database.execute("ALTER TABLE thread_delegations DROP COLUMN cancel_scope")
            elif damage == "receipt_key":
                database.execute("DROP TABLE thread_report_calls")
                database.execute(
                    "CREATE TABLE thread_report_calls (run_id TEXT, tool_call_id TEXT, delegation_id TEXT, payload_json TEXT, version INTEGER, PRIMARY KEY (tool_call_id, run_id))"
                )
            else:
                database.execute("DROP INDEX thread_delegations_git_namespace_claim")
                unique = "" if damage == "index_unique" else "UNIQUE"
                column = (
                    "parent_session_id" if damage == "index_column" else "git_artifact_namespace"
                )
                predicate = (
                    "git_artifacts_claimed = 0"
                    if damage == "index_predicate"
                    else "git_artifacts_claimed = 1"
                )
                database.execute(
                    f"CREATE {unique} INDEX thread_delegations_git_namespace_claim ON thread_delegations ({column}) WHERE {predicate}"
                )

        monkeypatch.setattr(tenant, "_ensure_user_db_schema", incomplete)
        with pytest.raises(RuntimeError, match="current schema is incomplete"):
            tenant._ensure_current_user_db_schema(connection)
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            connection.execute("SELECT name, sql FROM sqlite_master ORDER BY name").fetchall()
            == original_schema
        )
        assert connection.in_transaction is False
    finally:
        connection.close()
