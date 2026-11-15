"""Regression tests for Issue #98: Neo4j driver retry loop session lifecycle.

Callers in Neo4jStorage must acquire a new session instance from self._driver.session()
when retrying after a transient failure (SessionExpired, ServiceUnavailable, TransientError),
rather than reusing an invalidated/closed session that fails with ClientError.
"""

from unittest.mock import MagicMock, patch

import pytest
from neo4j.exceptions import (
    ClientError,
    ServiceUnavailable,
    SessionExpired,
    TransientError,
)

from app.storage.neo4j_storage import Neo4jStorage


class FakeRecord:
    def __init__(self, data: dict):
        self._data = data

    def __getitem__(self, key):
        return self._data[key]

    def get(self, key, default=None):
        return self._data.get(key, default)


class FakeResult:
    def __init__(self, records=None):
        self._records = [FakeRecord(r) if isinstance(r, dict) else r for r in (records or [])]

    def single(self):
        return self._records[0] if self._records else None

    def __iter__(self):
        return iter(self._records)


class MockSessionWithLifecycle:
    """Simulates a Neo4j session that becomes closed/invalidated after a transient failure."""

    def __init__(self, session_id: int, fail_error=None):
        self.session_id = session_id
        self.fail_error = fail_error
        self.closed = False
        self.execute_read_count = 0
        self.execute_write_count = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.closed = True

    def close(self):
        self.closed = True

    def execute_read(self, work, *args, **kwargs):
        if self.closed:
            raise ClientError("Cannot use closed session")
        self.execute_read_count += 1
        if self.fail_error and self.execute_read_count == 1:
            self.closed = True
            raise self.fail_error
        tx = MagicMock()
        tx.run.return_value = FakeResult([
            {"n": {"uuid": "test-uuid", "name": "Test Node"}, "labels": ["Entity"]}
        ])
        return work(tx, *args, **kwargs)

    def execute_write(self, work, *args, **kwargs):
        if self.closed:
            raise ClientError("Cannot use closed session")
        self.execute_write_count += 1
        if self.fail_error and self.execute_write_count == 1:
            self.closed = True
            raise self.fail_error
        tx = MagicMock()
        tx.run.return_value = FakeResult([])
        return work(tx, *args, **kwargs)

    def run(self, *args, **kwargs):
        if self.closed:
            raise ClientError("Cannot use closed session")
        return FakeResult([])


def _make_storage_with_driver_factory(session_factory):
    mock_driver = MagicMock()
    mock_driver.session.side_effect = session_factory

    with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=mock_driver):
        storage = Neo4jStorage(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            embedding_service=MagicMock(),
            ner_extractor=MagicMock(),
        )
    storage.RETRY_DELAY_BASE = 0.0
    return storage, mock_driver


@patch("time.sleep", return_value=None)
@pytest.mark.parametrize(
    "transient_error",
    [
        SessionExpired("Session expired and was closed"),
        ServiceUnavailable("Connection pool lost connection"),
        TransientError("Database unavailable temporarily"),
    ],
)
def test_execute_read_recovers_with_new_session_on_transient_failure(_mock_sleep, transient_error):
    """When execute_read encounters a transient error, the retry must acquire a fresh

    session from self._driver.session() and successfully complete the operation,
    rather than retrying against the invalidated/closed session.
    """
    sessions_created = []

    def session_factory():
        # First 2 sessions are used during storage.__init__ for schema setup
        # The 3rd session is the first attempt of get_node (which fails with transient_error)
        # The 4th session is the retry attempt (which should succeed)
        session_id = len(sessions_created) + 1
        fail_error = transient_error if session_id == 3 else None
        s = MockSessionWithLifecycle(session_id=session_id, fail_error=fail_error)
        sessions_created.append(s)
        return s

    storage, _mock_driver = _make_storage_with_driver_factory(session_factory)
    init_session_count = len(sessions_created)

    node = storage.get_node("test-uuid")

    assert node is not None
    assert node["uuid"] == "test-uuid"
    assert len(sessions_created) > init_session_count + 1, (
        "Expected retry to acquire a new session from driver.session(), "
        f"but only {len(sessions_created) - init_session_count} session(s) were created"
    )


@patch("time.sleep", return_value=None)
@pytest.mark.parametrize(
    "transient_error",
    [
        SessionExpired("Session expired and was closed"),
        ServiceUnavailable("Connection pool lost connection"),
        TransientError("Database unavailable temporarily"),
    ],
)
def test_execute_write_recovers_with_new_session_on_transient_failure(_mock_sleep, transient_error):
    """When execute_write encounters a transient error, the retry must acquire a fresh

    session from self._driver.session() and successfully complete the operation,
    rather than retrying against the invalidated/closed session.
    """
    sessions_created = []

    def session_factory():
        session_id = len(sessions_created) + 1
        fail_error = transient_error if session_id == 3 else None
        s = MockSessionWithLifecycle(session_id=session_id, fail_error=fail_error)
        sessions_created.append(s)
        return s

    storage, _mock_driver = _make_storage_with_driver_factory(session_factory)
    init_session_count = len(sessions_created)

    graph_id = storage.create_graph("test-graph", "desc")

    assert graph_id is not None
    assert len(sessions_created) > init_session_count + 1, (
        "Expected retry to acquire a new session from driver.session(), "
        f"but only {len(sessions_created) - init_session_count} session(s) were created"
    )


@patch("time.sleep", return_value=None)
def test_retry_exhaustion_raises_final_error_and_cleans_sessions(_mock_sleep):
    """When transient errors persist beyond MAX_RETRIES, the final error is raised

    and all sessions are cleanly closed without leaks.
    """
    sessions_created = []

    def session_factory():
        s = MockSessionWithLifecycle(session_id=len(sessions_created) + 1)
        sessions_created.append(s)
        return s

    storage, _mock_driver = _make_storage_with_driver_factory(session_factory)
    init_session_count = len(sessions_created)

    persistent_error = TransientError("Database persistently busy")

    def failing_work(tx):
        raise persistent_error

    with pytest.raises(TransientError) as exc_info:
        storage._execute_read(failing_work)

    assert exc_info.value is persistent_error

    # Should have attempted MAX_RETRIES times
    op_sessions = sessions_created[init_session_count:]
    assert len(op_sessions) == storage.MAX_RETRIES

    # Verify every session used during the retry loop was cleanly closed
    for s in op_sessions:
        assert s.closed is True


def test_non_transient_error_fails_immediately_without_retry():
    """Non-transient ClientError (e.g. Cypher syntax error or constraint violation)

    fails immediately on attempt 1 without retrying.
    """
    sessions_created = []

    def session_factory():
        s = MockSessionWithLifecycle(session_id=len(sessions_created) + 1)
        sessions_created.append(s)
        return s

    storage, _mock_driver = _make_storage_with_driver_factory(session_factory)
    init_session_count = len(sessions_created)

    client_error = ClientError("Invalid Cypher syntax")

    def failing_work(tx):
        raise client_error

    with pytest.raises(ClientError) as exc_info:
        storage._execute_write(failing_work)

    assert exc_info.value is client_error

    op_sessions = sessions_created[init_session_count:]
    assert len(op_sessions) == 1
    assert op_sessions[0].closed is True


@patch("time.sleep", return_value=None)
def test_mixed_transient_failures_followed_by_success(_mock_sleep):
    """Verify a session that fails with SessionExpired on attempt 1 and

    ServiceUnavailable on attempt 2 successfully returns on attempt 3 with fresh sessions.
    """
    sessions_created = []

    def session_factory():
        s = MockSessionWithLifecycle(session_id=len(sessions_created) + 1)
        sessions_created.append(s)
        return s

    storage, _mock_driver = _make_storage_with_driver_factory(session_factory)
    init_session_count = len(sessions_created)

    attempt_count = 0

    def work(tx):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise SessionExpired("Session expired on attempt 1")
        if attempt_count == 2:
            raise ServiceUnavailable("Service unavailable on attempt 2")
        return "success"

    result = storage._execute_read(work)

    assert result == "success"
    assert attempt_count == 3

    op_sessions = sessions_created[init_session_count:]
    assert len(op_sessions) == 3
    for s in op_sessions:
        assert s.closed is True


@patch("time.sleep")
@patch("random.uniform", side_effect=[0.1, 0.25])
def test_custom_backoff_delay_verification(mock_random, mock_sleep):
    """Verify exponential backoff is called with jitter between retry attempts."""
    sessions_created = []

    def session_factory():
        s = MockSessionWithLifecycle(session_id=len(sessions_created) + 1)
        sessions_created.append(s)
        return s

    storage, _mock_driver = _make_storage_with_driver_factory(session_factory)
    storage.RETRY_DELAY_BASE = 2.0

    attempt_count = 0

    def work(tx):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise TransientError(f"Transient error on attempt {attempt_count}")
        return "ok"

    result = storage._execute_write(work)

    assert result == "ok"
    assert attempt_count == 3

    # Attempt 0: wait = 2.0 * (2 ** 0) + 0.1 = 2.1
    # Attempt 1: wait = 2.0 * (2 ** 1) + 0.25 = 4.25
    assert mock_sleep.call_count == 2
    mock_sleep.assert_any_call(2.1)
    mock_sleep.assert_any_call(4.25)
