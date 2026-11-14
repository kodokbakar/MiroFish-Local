"""
Regression test for Item 066 (Issue #89):
Sanitize path identifiers to prevent arbitrary directory deletion and path traversal.

Verifies that path traversal identifiers (e.g. '../victim', '../../foo', '..')
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
