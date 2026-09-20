"""Authenticated HTTP workflow tests for minimal JSON suite tasks."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from mtp_contracts.results import CaseResult, RunState

from mtp_platform.service.executor import RunOutcome, summarize
from mtp_platform.service.jobs import JobManager
from mtp_platform.service.repository import RunRepository
from mtp_platform.web.app import _safe_child, create_app


def _suite(*cases: dict) -> bytes:
    return json.dumps({"cases": list(cases)}, ensure_ascii=False).encode()


VALID_CASE = {
    "schema_version": 1,
    "id": "WEB-001",
    "title": "valid web case",
    "steps": [{"id": "snapshot", "action": "playwright.snapshot"}],
}
VALID_SUITE = _suite(VALID_CASE)
INVALID_SUITE = _suite({"schema_version": 1, "id": "BROKEN-001", "title": "missing steps"})


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


def _fake_execute(_self, request, *, cancel_event=None, on_progress=None):
    results: list[CaseResult] = []
    for index, path in enumerate(request.case_paths, start=1):
        case = json.loads(path.read_text(encoding="utf-8"))
        result = CaseResult(
            run_id=request.run_id,
            case_id=case["id"],
            title=case["title"],
            status=RunState.PASSED,
            duration_ms=1,
        )
        results.append(result)
        if on_progress:
            on_progress(result, index, len(request.case_paths))
    return RunOutcome(request.run_id, results, summarize(results, cancelled=False))


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


def _post_suite(client: TestClient, *, content: bytes = VALID_SUITE, name: str = "test-suite.json"):
    token = _csrf(client.get("/runs/new"))
    return client.post(
        "/api/runs",
        files={"suite": (name, content, "application/json")},
        data={"csrf_token": token},
    )


def test_login_and_csrf(web_client):
    client, _app = web_client
    assert client.get("/api/runs").status_code == 401
    assert _login(client, password="wrong").status_code == 401
    assert _login(client).status_code == 303
    assert client.get("/runs/new").status_code == 200
    response = client.post(
        "/api/runs",
        files={"suite": ("test-suite.json", VALID_SUITE)},
        data={"csrf_token": "bad"},
    )
    assert response.status_code == 403


def test_one_test_suite_creates_only_minimal_sqlite_result(web_client):
    client, app = web_client
    _login(client)
    response = _post_suite(client, content=_suite(VALID_CASE, VALID_CASE | {"id": "WEB-002"}))
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", run_id)
    for _ in range(50):
        run = client.get(f"/api/runs/{run_id}").json()
        if run["status"] == "passed":
            break
        time.sleep(0.01)
    assert set(run) == {
        "run_id", "status", "created_at", "started_at", "finished_at",
        "cases_total", "cases_done", "summary", "first_failure", "cases", "evidence",
    }
    assert run["status"] == "passed"
    assert (run["cases_total"], run["cases_done"]) == (2, 2)
    assert run["summary"] == {"passed": 2, "failed": 0, "error": 0, "cancelled": 0}
    assert run["first_failure"] is None
    assert all(set(case) == {"case_id", "status", "duration_ms"} for case in run["cases"])
    assert not (app.state.artifacts_root / "runs" / run_id / "reports").exists()
    assert not list(app.state.artifacts_root.rglob("latest"))
    assert not list(app.state.artifacts_root.rglob("history.jsonl"))


@pytest.mark.parametrize(
    ("name", "content", "status_code", "code"),
    [
        ("my-suite.json", VALID_SUITE, 202, None),
        ("suite.txt", VALID_SUITE, 422, None),
        ("suite.json", b"not json", 422, "invalid_json"),
        ("suite.json", INVALID_SUITE, 422, "schema"),
        ("suite.json", _suite(VALID_CASE, VALID_CASE), 422, "duplicate_id"),
    ],
)
def test_accepts_any_json_filename_and_validates_its_contents(web_client, name, content, status_code, code):
    client, _app = web_client
    _login(client)
    response = _post_suite(client, name=name, content=content)
    assert response.status_code == status_code
    if code:
        assert response.json()["detail"]["errors"][0]["code"] == code


def test_rejects_multiple_suite_files(web_client):
    client, _app = web_client
    _login(client)
    token = _csrf(client.get("/runs/new"))
    response = client.post(
        "/api/runs",
        files=[("suite", ("test-suite.json", VALID_SUITE)), ("suite", ("test-suite.json", VALID_SUITE))],
        data={"csrf_token": token},
    )
    assert response.status_code == 400


def test_failure_and_png_evidence_are_stored_in_sqlite_and_require_login(web_client):
    client, app = web_client
    run_root = app.state.artifacts_root / "runs" / "known"
    image = run_root / "evidence" / "login" / "submit" / "failure.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\x89PNG\r\n\x1a\n")
    app.state.repository.create(run_id="known", uploads=[], options={})
    app.state.repository.update(
        "known",
        status="failed",
        cases_done=1,
        cases_json='[{"case_id": "login-success", "status": "failed", "duration_ms": 1234}]',
        summary_json='{"passed": 0, "failed": 1, "error": 0, "cancelled": 0}',
        first_failure_json='{"case_id": "login-success", "step_id": "submit-login", "message": "期望状态码 200，实际为 401"}',
        evidence_json='[{"path": "evidence/login/submit/failure.png", "mime_type": "image/png", "size": 8}]',
    )
    assert client.get("/api/runs/known/evidence/evidence/login/submit/failure.png").status_code == 401
    _login(client)
    run = client.get("/api/runs/known").json()
    assert run["first_failure"]["step_id"] == "submit-login"
    evidence = client.get(run["evidence"][0]["url"])
    assert evidence.headers["content-type"] == "image/png"
    assert client.get("/api/runs/known/evidence/../private.png").status_code == 404


def test_invalid_deployment_config_is_reported_when_creating_a_task(tmp_path, monkeypatch):
    monkeypatch.setenv("MTP_WEB_USERNAME", "admin")
    monkeypatch.setenv("MTP_WEB_PASSWORD", "test-password")
    monkeypatch.setenv("MTP_SESSION_SECRET", "test-session-secret-at-least-32-bytes")
    monkeypatch.setenv("MTP_ARTIFACT_ROOT", str(tmp_path / "artifacts"))
    app = create_app(config_path=str(tmp_path / "missing-config"))
    with TestClient(app) as client:
        _login(client)
        response = _post_suite(client)
    assert response.status_code == 503


def test_safe_child_blocks_path_escape(tmp_path):
    with pytest.raises(HTTPException):
        _safe_child(tmp_path / "safe", "../secret")


def test_repository_recovers_running_job_and_resumes_queued_json_cases(tmp_path, monkeypatch):
    repository = RunRepository(tmp_path / "runs.db")
    repository.create(run_id="running", uploads=[], options={})
    repository.update("running", status="running")
    repository.recover_interrupted()
    assert repository.get("running")["status"] == "error"

    upload = tmp_path / "case.json"
    upload.write_text(json.dumps(VALID_CASE), encoding="utf-8")
    repository.create(run_id="queued", uploads=[str(upload)], options={})
    monkeypatch.setattr("mtp_platform.service.jobs.RunExecutor.execute", _fake_execute)
    manager = JobManager(repository=repository, artifacts_root=tmp_path, config_path=None)
    manager.start()
    try:
        for _ in range(50):
            run = repository.get("queued")
            if run["status"] == "passed":
                break
            time.sleep(0.01)
        assert run["status"] == "passed"
    finally:
        manager.stop()


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_docker_compose_config_is_valid():
    result = subprocess.run(
        ["docker", "compose", "config"],
        cwd=Path(__file__).parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
