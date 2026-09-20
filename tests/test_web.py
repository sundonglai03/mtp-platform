from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from mtp_platform.service.executor import RunOutcome
from mtp_platform.service.jobs import JobManager
from mtp_platform.service.repository import RunRepository
from mtp_platform.web.app import _safe_child, create_app

VALID_CASE = Path("tests/cases/valid/api-login.yaml").read_bytes()
INVALID_CASE = b"schema_version: 1\nid: broken\ntitle: broken\n"


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match
    return match.group(1)


def _login(client: TestClient, *, password: str = "test-password"):
    token = _csrf(client.get("/login"))
    return client.post(
        "/login",
        data={"username": "admin", "password": password, "csrf_token": token},
        follow_redirects=False,
    )


def _fake_execute(_self, request, *, cancel_event=None, on_progress=None, emit=None):
    request.output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "json": request.output_dir / f"{request.run_id}-results.json",
        "junit": request.output_dir / f"{request.run_id}-junit.xml",
        "html": request.output_dir / f"{request.run_id}-report.html",
        "xlsx": None,
        "docx": None,
    }
    paths["json"].write_text("{}", encoding="utf-8")
    paths["junit"].write_text("<testsuite/>", encoding="utf-8")
    paths["html"].write_text("<html></html>", encoding="utf-8")
    cases = [
        {
            "run_id": request.run_id,
            "case_id": path.stem,
            "title": "fake",
            "status": "passed",
            "duration_ms": 1,
            "steps": [],
            "assertions": [],
        }
        for path in request.case_paths
    ]
    payload = {
        "run_id": request.run_id,
        "cases": cases,
        "summary": {
            "cases_total": len(cases),
            "cases_passed": len(cases),
            "cases_failed": 0,
            "cases_error": 0,
            "cases_cancelled": 0,
            "duration_ms": len(cases),
            "success": True,
        },
    }
    return RunOutcome(request.run_id, payload, paths)


@pytest.fixture()
def web_client(tmp_path, monkeypatch):
    monkeypatch.setenv("MTP_WEB_USERNAME", "admin")
    monkeypatch.setenv("MTP_WEB_PASSWORD", "test-password")
    monkeypatch.setenv("MTP_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("MTP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setattr("mtp_platform.service.jobs.RunExecutor.execute", _fake_execute)
    app = create_app()
    with TestClient(app) as client:
        yield client, app


def test_login_failure_success_and_logout(web_client):
    client, _app = web_client
    assert client.get("/api/runs").status_code == 401
    assert client.get("/docs").status_code == 404
    assert _login(client, password="wrong").status_code == 401
    assert _login(client).status_code == 303
    page = client.get("/")
    assert page.status_code == 200
    token = _csrf(page)
    response = client.post(
        "/logout", data={"csrf_token": token}, follow_redirects=False
    )
    assert response.status_code == 303
    assert client.get("/api/runs").status_code == 401


def test_login_and_mutations_require_csrf(web_client):
    client, _app = web_client
    assert (
        client.post(
            "/login",
            data={
                "username": "admin",
                "password": "test-password",
                "csrf_token": "bad",
            },
        ).status_code
        == 403
    )
    assert _login(client).status_code == 303
    response = client.post(
        "/api/runs",
        files={"files": ("case.yaml", VALID_CASE, "application/yaml")},
        data={"csrf_token": "bad"},
    )
    assert response.status_code == 403


def test_upload_run_complete_and_download_report(web_client):
    client, _app = web_client
    _login(client)
    token = _csrf(client.get("/runs/new"))
    response = client.post(
        "/api/runs",
        files={"files": ("case.yaml", VALID_CASE, "application/yaml")},
        data={"csrf_token": token},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    for _ in range(50):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] == "passed":
            break
        time.sleep(0.01)
    assert run["status"] == "passed"
    assert run["cases_done"] == 1
    report_url = run["report_urls"]["html"]
    assert client.get(report_url).status_code == 200


def test_mixed_upload_runs_only_valid_cases(web_client):
    client, _app = web_client
    _login(client)
    token = _csrf(client.get("/runs/new"))
    files = [
        ("files", ("valid.yaml", VALID_CASE, "application/yaml")),
        ("files", ("invalid.yaml", INVALID_CASE, "application/yaml")),
    ]
    response = client.post("/api/runs", files=files, data={"csrf_token": token})
    assert response.status_code == 202
    assert len(response.json()["validation_errors"]) == 1


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("bad.txt", VALID_CASE),
        ("../../case.yaml", VALID_CASE),
        ("mtp_config.yaml", VALID_CASE),
        ("bad.yaml", INVALID_CASE),
    ],
)
def test_rejects_invalid_uploads(web_client, filename, content):
    client, _app = web_client
    _login(client)
    token = _csrf(client.get("/runs/new"))
    response = client.post(
        "/api/runs",
        files={"files": (filename, content, "application/octet-stream")},
        data={"csrf_token": token},
    )
    assert response.status_code == 422


def test_rejects_too_many_uploads(web_client):
    client, _app = web_client
    _login(client)
    token = _csrf(client.get("/runs/new"))
    files = [
        ("files", (f"case-{index}.yaml", VALID_CASE, "application/yaml"))
        for index in range(21)
    ]
    response = client.post("/api/runs", files=files, data={"csrf_token": token})
    assert response.status_code == 400


def test_rejects_oversized_upload(tmp_path, monkeypatch):
    monkeypatch.setenv("MTP_WEB_USERNAME", "admin")
    monkeypatch.setenv("MTP_WEB_PASSWORD", "test-password")
    monkeypatch.setenv("MTP_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("MTP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    monkeypatch.setenv("MTP_MAX_UPLOAD_BYTES", "10")
    app = create_app()
    with TestClient(app) as client:
        _login(client)
        token = _csrf(client.get("/runs/new"))
        response = client.post(
            "/api/runs",
            files={"files": ("large.yaml", VALID_CASE, "application/yaml")},
            data={"csrf_token": token},
        )
    assert response.status_code == 413


def test_report_requires_login(web_client):
    client, app = web_client
    reports = app.state.artifacts_root / "runs" / "known" / "reports"
    reports.mkdir(parents=True)
    (reports / "report.html").write_text("ok", encoding="utf-8")
    app.state.repository.create(
        run_id="known", uploads=[], options={}, validation_errors=[]
    )
    app.state.repository.update(
        "known", reports_json='{"html": "report.html"}', status="passed"
    )
    assert client.get("/api/runs/known/reports/report.html").status_code == 401


def test_authenticated_evidence_download(web_client):
    client, app = web_client
    evidence = (
        app.state.artifacts_root / "runs" / "evidence-run" / "evidence" / "proof.txt"
    )
    evidence.parent.mkdir(parents=True)
    evidence.write_text("proof", encoding="utf-8")
    app.state.repository.create(
        run_id="evidence-run", uploads=[], options={}, validation_errors=[]
    )
    _login(client)
    run = client.get("/api/runs/evidence-run").json()
    assert run["evidence"][0]["path"] == "evidence/proof.txt"
    assert client.get(run["evidence"][0]["url"]).text == "proof"
    assert (
        client.get(
            "/api/runs/evidence-run/evidence/../_audit/private.jsonl"
        ).status_code
        == 404
    )


def test_safe_child_blocks_path_escape(tmp_path):
    with pytest.raises(HTTPException):
        _safe_child(tmp_path / "safe", "../secret")


def test_repository_recovers_running_job(tmp_path):
    repository = RunRepository(tmp_path / "runs.db")
    repository.create(run_id="run-1", uploads=[], options={}, validation_errors=[])
    repository.update("run-1", status="running")
    repository.recover_interrupted()
    recovered = repository.get("run-1")
    assert recovered["status"] == "error"
    assert "重启" in recovered["error"]


def test_job_manager_resumes_queued_job(tmp_path, monkeypatch):
    repository = RunRepository(tmp_path / "runs.db")
    upload = tmp_path / "case.yaml"
    upload.write_bytes(VALID_CASE)
    repository.create(
        run_id="queued-restart", uploads=[str(upload)], options={}, validation_errors=[]
    )
    monkeypatch.setattr("mtp_platform.service.jobs.RunExecutor.execute", _fake_execute)
    manager = JobManager(
        repository=repository,
        artifacts_root=tmp_path,
        config_path=None,
        max_workers=1,
    )
    manager.start()
    try:
        for _ in range(50):
            run = repository.get("queued-restart")
            if run["status"] == "passed":
                break
            time.sleep(0.01)
        assert run["status"] == "passed"
    finally:
        manager.stop()


def test_cancel_queued_and_running_jobs(tmp_path):
    repository = RunRepository(tmp_path / "runs.db")
    manager = JobManager(
        repository=repository,
        artifacts_root=tmp_path,
        config_path=None,
        max_workers=1,
    )
    repository.create(run_id="queued", uploads=[], options={}, validation_errors=[])
    assert manager.cancel("queued") is True
    assert repository.get("queued")["status"] == "cancelled"

    repository.create(run_id="running", uploads=[], options={}, validation_errors=[])
    repository.update("running", status="running")
    event = __import__("threading").Event()
    manager._active["running"] = event
    assert manager.cancel("running") is True
    assert event.is_set()


@pytest.mark.skipif(
    shutil.which("docker") is None, reason="Docker CLI is not installed"
)
def test_docker_compose_config_is_valid():
    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
