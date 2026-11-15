"""
Regression tests for GraphMemoryUpdater clean shutdown and lock contention (Issue #100).
"""

import threading
import time
from unittest.mock import MagicMock

from app.services.graph_memory_updater import AgentActivity, GraphMemoryUpdater


def test_send_batch_activities_not_holding_buffer_lock():
    """
    Test that _send_batch_activities() is called WITHOUT holding _buffer_lock.

    Holding _buffer_lock during blocking network I/O or sleep backoff prevents
    stop(), get_stats(), or other threads from accessing buffers.
    """
    storage = MagicMock()
    updater = GraphMemoryUpdater(graph_id="test_graph", storage=storage)

    lock_held_during_send = False

    def fake_send(batch, platform):
        nonlocal lock_held_during_send
        # If _buffer_lock is held by the caller, acquire(blocking=False) will return False
        acquired = updater._buffer_lock.acquire(blocking=False)
        if acquired:
            updater._buffer_lock.release()
        else:
            lock_held_during_send = True

    updater._send_batch_activities = fake_send

    # Add BATCH_SIZE activities to trigger immediate batch send in worker loop
    updater.BATCH_SIZE = 2
    updater.SEND_INTERVAL = 0.01

    act1 = AgentActivity("twitter", 1, "Alice", "CREATE_POST", {"content": "hi"}, 1, "2026-01-01T00:00:00")
    act2 = AgentActivity("twitter", 2, "Bob", "CREATE_POST", {"content": "hello"}, 1, "2026-01-01T00:00:00")

    updater.start()
    try:
        updater.add_activity(act1)
        updater.add_activity(act2)

        # Wait briefly for batch to process
        deadline = time.time() + 2.0
        while time.time() < deadline:
            with updater._buffer_lock:
                if len(updater._platform_buffers.get("twitter", [])) == 0 and updater._activity_queue.empty():
                    break
            time.sleep(0.05)
    finally:
        updater.stop()

    assert not lock_held_during_send, "_send_batch_activities() was called while holding _buffer_lock"


def test_stop_cleanly_halts_worker_without_lock_contention_or_thread_leak():
    """
    Test that stop() does not deadlock or time out while worker is blocked in _send_batch_activities.

    When worker is inside _send_batch_activities(), calling stop() should not block
    on _buffer_lock inside _flush_remaining() while worker holds the lock, causing join(timeout=10)
    to wait unnecessarily or leak threads.
    """
    storage = MagicMock()
    updater = GraphMemoryUpdater(graph_id="test_graph", storage=storage)
    updater.BATCH_SIZE = 1
    updater.SEND_INTERVAL = 0.01

    in_send_event = threading.Event()
    continue_send_event = threading.Event()

    def slow_send(batch, platform):
        in_send_event.set()
        continue_send_event.wait(timeout=2.0)

    updater._send_batch_activities = slow_send

    act = AgentActivity("twitter", 1, "Alice", "CREATE_POST", {"content": "hi"}, 1, "2026-01-01T00:00:00")

    updater.start()
    updater.add_activity(act)

    assert in_send_event.wait(timeout=2.0), "Worker did not reach _send_batch_activities"

    # Worker is now in _send_batch_activities.
    # While worker is blocked in I/O, other threads should be able to acquire _buffer_lock
    # without waiting for the slow I/O to finish.
    buffer_lock_acquired = updater._buffer_lock.acquire(blocking=False)
    if buffer_lock_acquired:
        updater._buffer_lock.release()

    continue_send_event.set()
    updater.stop()

    assert not updater._worker_thread.is_alive(), "Worker thread leaked or remained alive after stop()"
    assert buffer_lock_acquired, "_buffer_lock was held during slow I/O, causing contention"


def test_rapid_start_stop_cycles():
    """Verify repeatedly starting and stopping GraphMemoryUpdater does not leak threads or hang."""
    storage = MagicMock()
    threads_before = threading.active_count()

    for i in range(10):
        updater = GraphMemoryUpdater(graph_id=f"test_rapid_{i}", storage=storage)
        updater.start()
        updater.stop()
        assert updater._worker_thread is not None
        assert not updater._worker_thread.is_alive()

    # Small pause to allow thread termination cleanup in runtime
    time.sleep(0.05)
    threads_after = threading.active_count()
    assert threads_after <= threads_before + 1


def test_stop_idempotent():
    """Verify calling stop() multiple times does not throw exceptions or corrupt state."""
    storage = MagicMock()
    updater = GraphMemoryUpdater(graph_id="test_idempotent", storage=storage)
    updater.start()

    # First stop
    updater.stop()
    assert not updater._running
    assert not updater._worker_thread.is_alive()

    # Second stop
    updater.stop()
    assert not updater._running

    # Also test stop() on an updater that was never started
    unstarted = GraphMemoryUpdater(graph_id="test_unstarted", storage=storage)
    unstarted.stop()
    assert not unstarted._running


def test_empty_queue_shutdown_fast():
    """Verify stopping an idle updater exits promptly without waiting for queue timeout."""
    storage = MagicMock()
    updater = GraphMemoryUpdater(graph_id="test_fast_shutdown", storage=storage)
    updater.start()

    start_time = time.monotonic()
    updater.stop()
    duration = time.monotonic() - start_time

    assert not updater._worker_thread.is_alive()
    # Sentinel None should wake worker immediately (worker queue timeout is 1.0s)
    assert duration < 0.5


def test_high_volume_flush_without_loss():
    """Verify 500+ items across multiple platforms are completely flushed without loss upon stop()."""
    storage = MagicMock()
    updater = GraphMemoryUpdater(graph_id="test_high_volume", storage=storage)
    updater.BATCH_SIZE = 10
    updater.SEND_INTERVAL = 0.001

    flushed_items = []
    lock = threading.Lock()

    def record_send(batch, platform):
        with lock:
            flushed_items.extend(batch)

    updater._send_batch_activities = record_send

    updater.start()

    total_items = 600
    for i in range(total_items):
        platform = "twitter" if i % 2 == 0 else "reddit"
        act = AgentActivity(
            platform=platform,
            agent_id=i,
            agent_name=f"Agent_{i}",
            action_type="CREATE_POST",
            action_args={"content": f"msg {i}"},
            round_num=1,
            timestamp="2026-01-01T00:00:00",
        )
        updater.add_activity(act)

    updater.stop()

    assert not updater._worker_thread.is_alive()
    assert len(flushed_items) == total_items

    flushed_ids = {item.agent_id for item in flushed_items}
    assert flushed_ids == set(range(total_items))
