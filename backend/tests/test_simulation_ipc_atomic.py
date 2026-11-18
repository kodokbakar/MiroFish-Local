"""
Regression tests for Issue #101:
Atomic IPC command writing and isolated exception handling to prevent premature run exits.
"""

import json
import os
import sys
import threading
import time
from unittest.mock import AsyncMock, patch

import pytest

# Ensure backend and backend/scripts are in sys.path
_backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_scripts_dir = os.path.join(_backend_dir, "scripts")
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from run_parallel_simulation import ParallelIPCHandler

from app.services.simulation_ipc import (
    CommandStatus,
    CommandType,
    IPCResponse,
    SimulationIPCClient,
    SimulationIPCServer,
)


def _write_json(path: str, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _read_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


class TestSimulationIPCAtomicWrites:
    """Tests ensuring command, response, and status writes are performed atomically via .tmp and os.replace."""

    def test_simulation_ipc_client_send_command_atomic_write(self, tmp_path):
        """
        SimulationIPCClient.send_command should write the command to a temporary (.tmp) file
        first and atomically rename it via os.replace to prevent partial reads by the simulation runner.
        """
        client = SimulationIPCClient(str(tmp_path))

        # Background thread to provide response so send_command completes
        def reply():
            start = time.time()
            while time.time() - start < 2.0:
                files = [f for f in os.listdir(client.commands_dir) if f.endswith(".json")]
                if files:
                    cmd_id = files[0].replace(".json", "")
                    resp = IPCResponse(
                        command_id=cmd_id,
                        status=CommandStatus.COMPLETED,
                        result={"success": True},
                    )
                    resp_file = os.path.join(client.responses_dir, f"{cmd_id}.json")
                    _write_json(resp_file, resp.to_dict())
                    break
                time.sleep(0.01)

        t = threading.Thread(target=reply)
        t.start()

        with patch("os.replace", wraps=os.replace) as mock_replace:
            resp = client.send_command(
                CommandType.INTERVIEW,
                {"agent_id": 0, "prompt": "hello"},
                timeout=2.0,
            )

        t.join()
        assert resp.status == CommandStatus.COMPLETED

        # Verify atomic write via os.replace was performed for command file
        command_replaces = [
            call for call in mock_replace.call_args_list
            if len(call[0]) >= 2 and call[0][1].endswith(".json") and client.commands_dir in call[0][1]
        ]
        assert len(command_replaces) >= 1, (
            "SimulationIPCClient.send_command must write via a temporary file and os.replace"
        )
        src, dst = command_replaces[0][0][:2]
        assert src.endswith(".tmp"), f"Atomic write source should be a .tmp file, got: {src}"
        assert dst.endswith(".json"), f"Atomic write destination should be a .json file, got: {dst}"

    def test_parallel_ipc_handler_send_response_atomic_write(self, tmp_path):
        """
        ParallelIPCHandler.send_response should write responses to a temporary (.tmp) file
        first and atomically rename via os.replace to prevent Flask client from reading partial JSON.
        """
        handler = ParallelIPCHandler(str(tmp_path))

        with patch("os.replace", wraps=os.replace) as mock_replace:
            handler.send_response(
                command_id="cmd-atomic-test",
                status="completed",
                result={"result": "ok"},
            )

        response_replaces = [
            call for call in mock_replace.call_args_list
            if len(call[0]) >= 2 and call[0][1].endswith(".json") and handler.responses_dir in call[0][1]
        ]
        assert len(response_replaces) >= 1, (
            "ParallelIPCHandler.send_response must write via a temporary file and os.replace"
        )
        src, dst = response_replaces[0][0][:2]
        assert src.endswith(".tmp"), f"Response atomic source should be a .tmp file, got: {src}"
        assert dst.endswith(".json"), f"Response atomic destination should be a .json file, got: {dst}"

    def test_parallel_ipc_handler_update_status_atomic_write(self, tmp_path):
        """
        ParallelIPCHandler.update_status should write status to a temporary (.tmp) file
        first and atomically rename via os.replace.
        """
        handler = ParallelIPCHandler(str(tmp_path))

        with patch("os.replace", wraps=os.replace) as mock_replace:
            handler.update_status("alive")

        status_replaces = [
            call for call in mock_replace.call_args_list
            if len(call[0]) >= 2 and call[0][1] == handler.status_file
        ]
        assert len(status_replaces) >= 1, (
            "ParallelIPCHandler.update_status must write via a temporary file and os.replace"
        )
        src, dst = status_replaces[0][0][:2]
        assert src.endswith(".tmp"), f"Status atomic source should be a .tmp file, got: {src}"
        assert dst == handler.status_file

    def test_simulation_ipc_server_send_response_atomic_write(self, tmp_path):
        """
        SimulationIPCServer.send_response should write response via a temporary (.tmp) file
        first and atomically rename via os.replace.
        """
        server = SimulationIPCServer(str(tmp_path))

        with patch("os.replace", wraps=os.replace) as mock_replace:
            server.send_response(
                IPCResponse(
                    command_id="cmd-server-atomic",
                    status=CommandStatus.COMPLETED,
                    result={"ok": True},
                )
            )

        response_replaces = [
            call for call in mock_replace.call_args_list
            if len(call[0]) >= 2 and call[0][1].endswith(".json") and server.responses_dir in call[0][1]
        ]
        assert len(response_replaces) >= 1, (
            "SimulationIPCServer.send_response must write via a temporary file and os.replace"
        )
        src, dst = response_replaces[0][0][:2]
        assert src.endswith(".tmp"), f"Server response atomic source should be a .tmp file, got: {src}"
        assert dst.endswith(".json"), f"Server response atomic destination should be a .json file, got: {dst}"


class TestSimulationIPCExceptionIsolation:
    """Tests ensuring process_commands isolates exceptions and returns error responses instead of crashing."""

    @pytest.mark.asyncio
    async def test_process_commands_isolates_handler_exception_and_sends_error_response(self, tmp_path):
        """
        When command execution/dispatch encounters an unhandled exception (e.g. LLM runtime error),
        process_commands must isolate the error, send an error response with status='failed',
        and return True so the simulation main loop does not terminate prematurely.
        """
        handler = ParallelIPCHandler(str(tmp_path))
        command_id = "cmd-crash-1"

        # Write command file
        cmd_file = os.path.join(handler.commands_dir, f"{command_id}.json")
        _write_json(cmd_file, {
            "command_id": command_id,
            "command_type": "interview",
            "args": {"agent_id": 1, "prompt": "test question"},
        })

        # Mock handle_interview to raise an unhandled exception
        handler.handle_interview = AsyncMock(side_effect=RuntimeError("Simulated LLM service crash"))

        # process_commands must isolate the error and return True to keep main loop running
        should_continue = await handler.process_commands()

        assert should_continue is True, (
            "process_commands must return True on dispatch exceptions to prevent premature simulation exit"
        )

        # An error response must be written for the command
        resp_file = os.path.join(handler.responses_dir, f"{command_id}.json")
        assert os.path.exists(resp_file), "process_commands must write an error response on failure"

        resp_data = _read_json(resp_file)
        assert resp_data["command_id"] == command_id
        assert resp_data["status"] == "failed"
        assert "Simulated LLM service crash" in str(resp_data.get("error"))

    @pytest.mark.asyncio
    async def test_process_commands_isolates_malformed_command_payload(self, tmp_path):
        """
        When a command file has malformed payload (e.g. args=None or non-dict args),
        process_commands must not raise AttributeError/TypeError, but instead isolate the error,
        send a failed response, and return True to keep running.
        """
        handler = ParallelIPCHandler(str(tmp_path))
        command_id = "cmd-malformed-1"

        # Write malformed command with args: null
        cmd_file = os.path.join(handler.commands_dir, f"{command_id}.json")
        _write_json(cmd_file, {
            "command_id": command_id,
            "command_type": "interview",
            "args": None,
        })

        should_continue = await handler.process_commands()

        assert should_continue is True, (
            "process_commands must isolate malformed command errors and return True"
        )

        # An error response must be recorded
        resp_file = os.path.join(handler.responses_dir, f"{command_id}.json")
        assert os.path.exists(resp_file), "process_commands must write an error response for malformed command"

        resp_data = _read_json(resp_file)
        assert resp_data["status"] == "failed"
        assert resp_data["command_id"] == command_id

    @pytest.mark.asyncio
    async def test_simulation_main_loop_survives_failed_command(self, tmp_path):
        """
        Demonstrates that a simulation runner loop survives a failing command and
        successfully continues to process subsequent valid commands.
        """
        handler = ParallelIPCHandler(str(tmp_path))

        # First command is bad/failing
        cmd1_id = "cmd-bad"
        cmd1_file = os.path.join(handler.commands_dir, f"{cmd1_id}.json")
        _write_json(cmd1_file, {
            "command_id": cmd1_id,
            "command_type": "interview",
            "args": None,
        })

        # Processing bad command must not raise exception
        c1_continue = await handler.process_commands()
        assert c1_continue is True

        # Second command is valid
        cmd2_id = "cmd-good"
        cmd2_file = os.path.join(handler.commands_dir, f"{cmd2_id}.json")
        _write_json(cmd2_file, {
            "command_id": cmd2_id,
            "command_type": "interview",
            "args": {"agent_id": 2, "prompt": "valid prompt"},
        })

        handler.handle_interview = AsyncMock(return_value=True)
        c2_continue = await handler.process_commands()
        assert c2_continue is True
        assert handler.handle_interview.await_count == 1
