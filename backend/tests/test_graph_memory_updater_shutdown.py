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
