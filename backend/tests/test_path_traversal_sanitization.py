"""
Regression and edge-case tests for Item 066 (Issue #89):
Sanitize path identifiers to prevent arbitrary directory deletion and path traversal.

Verifies that path traversal identifiers (e.g. '../victim', '../../foo', '..'),
URL-encoded traversal, null bytes, absolute paths, and external symlinks
are rejected with ValueError/400 and do not resolve outside expected root directories
or delete arbitrary directories.
"""

import os
import tempfile

import pytest
from flask import Flask

from app.api.graph import delete_project
from app.api.report import delete_report
from app.models.project import ProjectManager
from app.services.report_agent import ReportManager
from app.services.simulation_manager import SimulationManager
from app.utils.path_security import get_safe_path, validate_safe_id


@pytest.fixture
def isolated_storage():
    """Create isolated temporary directory structure for storage testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        uploads = os.path.join(tmpdir, "uploads")
        projects_dir = os.path.join(uploads, "projects")
        reports_dir = os.path.join(uploads, "reports")
        simulations_dir = os.path.join(uploads, "simulations")
        victim_dir = os.path.join(uploads, "victim_dir")

        os.makedirs(projects_dir, exist_ok=True)
        os.makedirs(reports_dir, exist_ok=True)
        os.makedirs(simulations_dir, exist_ok=True)
        os.makedirs(victim_dir, exist_ok=True)

        old_projects_dir = ProjectManager.PROJECTS_DIR
        old_reports_dir = ReportManager.REPORTS_DIR

        ProjectManager.PROJECTS_DIR = projects_dir
        ReportManager.REPORTS_DIR = reports_dir

        sim_manager = SimulationManager()
        sim_manager.SIMULATION_DATA_DIR = simulations_dir

        yield {
            "root": tmpdir,
            "uploads": uploads,
            "projects_dir": projects_dir,
            "reports_dir": reports_dir,
            "simulations_dir": simulations_dir,
            "victim_dir": victim_dir,
            "sim_manager": sim_manager,
        }

        ProjectManager.PROJECTS_DIR = old_projects_dir
        ReportManager.REPORTS_DIR = old_reports_dir


def test_project_manager_get_project_dir_path_traversal(isolated_storage):
    """
    ProjectManager._get_project_dir should reject path traversal identifiers
    and raise ValueError, preventing paths resolving outside PROJECTS_DIR.
    """
    traversal_ids = ["..", "../victim_dir", "../../foo", "sub/dir", "/etc/passwd"]
    for bad_id in traversal_ids:
        with pytest.raises(ValueError):
            ProjectManager._get_project_dir(bad_id)


def test_report_manager_get_report_folder_path_traversal(isolated_storage):
    """
    ReportManager._get_report_folder should reject path traversal identifiers
    and raise ValueError, preventing paths resolving outside REPORTS_DIR.
    """
    traversal_ids = ["..", "../victim_dir", "../../foo", "sub/dir", "/etc/passwd"]
    for bad_id in traversal_ids:
        with pytest.raises(ValueError):
            ReportManager._get_report_folder(bad_id)


def test_simulation_manager_get_simulation_dir_path_traversal(isolated_storage):
    """
    SimulationManager._get_simulation_dir should reject path traversal identifiers
    and raise ValueError, preventing directories created outside SIMULATION_DATA_DIR.
    """
    sim_manager = isolated_storage["sim_manager"]
    traversal_ids = ["..", "../victim_dir", "../../foo", "sub/dir", "/etc/passwd"]
    for bad_id in traversal_ids:
        with pytest.raises(ValueError):
            sim_manager._get_simulation_dir(bad_id)


def test_delete_project_prevents_arbitrary_directory_deletion(isolated_storage):
    """
    delete_project should reject path traversal IDs (or return 400 Bad Request)
    and not delete directories outside PROJECTS_DIR.
    """
    app = Flask("test_app")
    victim_dir = isolated_storage["victim_dir"]
    assert os.path.exists(victim_dir)

    with app.app_context():
        response = delete_project("../victim_dir")
        # If API handles it, response status code must be 400 (Bad Request)
        status_code = getattr(response, "status_code", None)
        if isinstance(response, tuple):
            status_code = response[1]
        assert status_code == 400, f"Expected 400 Bad Request, got: {status_code}"

    # Directory must not be deleted
    assert os.path.exists(victim_dir), "Vulnerability reproduced: arbitrary directory was deleted!"


def test_delete_report_prevents_arbitrary_directory_deletion(isolated_storage):
    """
    delete_report should reject path traversal IDs (or return 400 Bad Request)
    and not delete directories outside REPORTS_DIR.
    """
    app = Flask("test_app")
    victim_dir = isolated_storage["victim_dir"]
    assert os.path.exists(victim_dir)

    with app.app_context():
        response = delete_report("../victim_dir")
        # If API handles it, response status code must be 400 (Bad Request)
        status_code = getattr(response, "status_code", None)
        if isinstance(response, tuple):
            status_code = response[1]
        assert status_code == 400, f"Expected 400 Bad Request, got: {status_code}"

    # Directory must not be deleted
    assert os.path.exists(victim_dir), "Vulnerability reproduced: arbitrary directory was deleted!"


def test_url_encoded_path_traversal_rejection(isolated_storage):
    """
    URL-encoded path traversal sequences must be rejected by validation,
    path resolution, and API endpoints.
    """
    app = Flask("test_app")
    victim_dir = isolated_storage["victim_dir"]
    encoded_sequences = [
        "%2e%2e%2f",
        "..%2f",
        "%2e%2e/",
        "..%252f",
        "%252e%252e%252f",
        "..%c0%af",
        "..%5c",
        "%2e%2e%5cvictim_dir",
        "..%2fvictim_dir",
    ]
    for bad_id in encoded_sequences:
        with pytest.raises(ValueError):
            validate_safe_id(bad_id)

        with pytest.raises(ValueError):
            get_safe_path(isolated_storage["projects_dir"], bad_id)

        with pytest.raises(ValueError):
            ProjectManager._get_project_dir(bad_id)

        with pytest.raises(ValueError):
            ReportManager._get_report_folder(bad_id)

        sim_manager = isolated_storage["sim_manager"]
        with pytest.raises(ValueError):
            sim_manager._get_simulation_dir(bad_id)

    with app.app_context():
        resp = delete_project("..%2fvictim_dir")
        status_code = resp[1] if isinstance(resp, tuple) else resp.status_code
        assert status_code == 400
        assert os.path.exists(victim_dir)

        resp = delete_report("%2e%2e%2fvictim_dir")
        status_code = resp[1] if isinstance(resp, tuple) else resp.status_code
        assert status_code == 400
        assert os.path.exists(victim_dir)


def test_null_bytes_rejection(isolated_storage):
    """
    Null bytes in identifiers must be rejected with ValueError and return 400 in APIs.
    """
    app = Flask("test_app")
    null_byte_ids = [
        "foo\x00bar",
        "\x00",
        "project\x00",
        "\x00admin",
        "../\x00victim",
    ]
    for bad_id in null_byte_ids:
        with pytest.raises(ValueError):
            validate_safe_id(bad_id)

        with pytest.raises(ValueError):
            get_safe_path(isolated_storage["projects_dir"], bad_id)

    with app.app_context():
        resp = delete_project("foo\x00bar")
        status_code = resp[1] if isinstance(resp, tuple) else resp.status_code
        assert status_code == 400


def test_absolute_paths_rejection(isolated_storage):
    """
    Absolute paths (Unix and Windows style) must be rejected with ValueError.
    """
    absolute_paths = [
        "/etc/passwd",
        "/root",
        "/var/log",
        "/",
        "C:\\Windows",
        "C:\\Windows\\System32",
        "\\\\server\\share",
        "D:\\data",
    ]
    for abs_path in absolute_paths:
        with pytest.raises(ValueError):
            validate_safe_id(abs_path)

        with pytest.raises(ValueError):
            get_safe_path(isolated_storage["projects_dir"], abs_path)

        with pytest.raises(ValueError):
            ProjectManager._get_project_dir(abs_path)


def test_symlink_traversal_rejection(isolated_storage):
    """
    If a symlink inside base_dir points outside base_dir, get_safe_path must reject it
    with ValueError. Symlinks pointing inside base_dir should be accepted.
    """
    projects_dir = isolated_storage["projects_dir"]
    victim_dir = isolated_storage["victim_dir"]

    # Create symlink pointing outside projects_dir to victim_dir
    symlink_outside = os.path.join(projects_dir, "sym_outside")
    os.symlink(victim_dir, symlink_outside)

    # Identifier string is valid alphanumeric, but realpath points outside projects_dir
    assert validate_safe_id("sym_outside") == "sym_outside"
    with pytest.raises(ValueError, match="Path traversal detected"):
        get_safe_path(projects_dir, "sym_outside")

    # ProjectManager._get_project_dir should also reject it
    with pytest.raises(ValueError, match="Path traversal detected"):
        ProjectManager._get_project_dir("sym_outside")

    # Now create internal directory and symlink pointing inside projects_dir
    internal_target = os.path.join(projects_dir, "real_target")
    os.makedirs(internal_target, exist_ok=True)
    symlink_inside = os.path.join(projects_dir, "sym_inside")
    os.symlink(internal_target, symlink_inside)

    safe_path = get_safe_path(projects_dir, "sym_inside")
    assert safe_path == os.path.join(projects_dir, "sym_inside")


def test_valid_identifiers_and_boundaries(isolated_storage):
    """
    Valid identifiers with single-char, boundary lengths, hyphens, and underscores
    should be accepted and resolve safely within base_dir.
    """
    projects_dir = isolated_storage["projects_dir"]
    valid_ids = [
        "a",
        "1",
        "_",
        "-",
        "proj-1",
        "proj_1",
        "report_2024_01_01",
        "sim-test-run-01",
        "123e4567-e89b-12d3-a456-426614174000",
        "a" * 64,
        "b" * 128,
        "c" * 255,
    ]
    for valid_id in valid_ids:
        assert validate_safe_id(valid_id) == valid_id
        safe_path = get_safe_path(projects_dir, valid_id)
        assert safe_path == os.path.join(projects_dir, valid_id)
        assert ProjectManager._get_project_dir(valid_id) == os.path.join(projects_dir, valid_id)


def test_invalid_types_and_empty_identifiers(isolated_storage):
    """
    Empty strings and non-string types must be rejected with ValueError.
    """
    for invalid in ["", None, 123, [], {}, 0]:
        with pytest.raises(ValueError):
            validate_safe_id(invalid)  # type: ignore[arg-type]
        with pytest.raises(ValueError):
            get_safe_path(isolated_storage["projects_dir"], invalid)  # type: ignore[arg-type]
