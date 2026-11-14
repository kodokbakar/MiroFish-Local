"""Tests for Issue #76: process group isolation and clean child termination."""

import os
import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from app.services.simulation_runner import SimulationRunner, IS_WINDOWS


def test_terminate_process_handles_process_lookup_error_gracefully():
    """If process terminates right before getpgid, ProcessLookupError must not escape."""
    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.poll.return_value = None

    with patch("os.getpgid", side_effect=ProcessLookupError), \
         patch("app.services.simulation_runner.IS_WINDOWS", False):
        # Should not raise exception
        SimulationRunner._terminate_process(mock_proc, "test_sim")


def test_terminate_process_escalates_to_sigkill_on_timeout():
    """If process group does not respond to SIGTERM within timeout, SIGKILL must be sent."""
    mock_proc = MagicMock()
    mock_proc.pid = 12345
    mock_proc.poll.return_value = None
    mock_proc.wait.side_effect = [subprocess.TimeoutExpired(cmd="run", timeout=1), None]

    signals_sent = []

    def mock_killpg(pgid, sig):
        signals_sent.append((pgid, sig))

    with patch("os.getpgid", return_value=12345), \
         patch("os.killpg", side_effect=mock_killpg), \
         patch("app.services.simulation_runner.IS_WINDOWS", False):
        SimulationRunner._terminate_process(mock_proc, "test_sim", timeout=1)

    assert (12345, signal.SIGTERM) in signals_sent
    assert (12345, signal.SIGKILL) in signals_sent


def test_popen_uses_start_new_session_or_creationflags():
    """Verify popen launch flags are set correctly across Unix and Windows."""
    extra_kwargs = SimulationRunner._get_process_group_kwargs()
    if IS_WINDOWS:
        assert extra_kwargs.get("creationflags") == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert extra_kwargs.get("start_new_session") is True
