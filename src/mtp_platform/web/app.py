"""FastAPI application for uploading, running and reviewing test cases."""

from __future__ import annotations

import os
import secrets
import shutil
import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from mtp_contracts.case_validator import validate_case
from mtp_contracts.results import new_run_id
from starlette.middleware.sessions import SessionMiddleware

from mtp_platform.config import load_config
from mtp_platform.service.jobs import JobManager
from mtp_platform.service.repository import RunRepository

WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
SUITE_FILENAME = "test-suite.json"


def _positive_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必须是整数") from exc
    if value < 1:
        raise RuntimeError(f"{name} 必须大于 0")
    return value


def _csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return str(token)


def _verify_csrf(request: Request, supplied: str | None) -> None:
    expected = request.session.get("csrf_token")
    if (
        not expected
        or not supplied
        or not secrets.compare_digest(str(expected), supplied)
    ):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")


def _require_user(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=401, detail="请先登录")
    return str(username)


def _safe_child(root: Path, relative: str) -> Path:
    base = root.resolve()
    candidate = (base / relative).resolve()
    if candidate == base or not candidate.is_relative_to(base):
        raise HTTPException(status_code=404, detail="文件不存在")
    return candidate


def _suite_error(
    *,
    case_index: int | None,
    case_id: str | None,
    path: str,
    code: str,
    message: str,
) -> dict[str, Any]:
    return {
        "case_index": case_index,
        "case_id": case_id,
        "path": path,
        "code": code,
        "message": message,
    }


def _validate_suite(payload: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate one JSON suite before a background task is created."""
    if not isinstance(payload, dict):
        return [], [
            _suite_error(
                case_index=None,
                case_id=None,
                path="",
                code="invalid_suite_type",
                message="测试套件根节点必须是 JSON 对象",
            )
        ]
    cases = payload.get("cases")
    if not isinstance(cases, list) or not cases:
        return [], [
            _suite_error(
                case_index=None,
                case_id=None,
                path="cases",
                code="required",
                message="cases 必填且不能为空",
            )
        ]

    errors: list[dict[str, Any]] = []
    valid_cases: list[dict[str, Any]] = []
    seen_ids: dict[str, int] = {}
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            errors.append(
                _suite_error(
                    case_index=index,
                    case_id=None,
                    path="",
                    code="invalid_case_type",
                    message="用例必须是 JSON 对象",
                )
            )
            continue
        case_id = case.get("id") if isinstance(case.get("id"), str) else None
        for issue in validate_case(case).issues:
            errors.append(
                _suite_error(
                    case_index=index,
                    case_id=case_id,
                    path=issue.path,
                    code=issue.kind,
                    message=issue.message,
                )
            )
        if case_id is not None:
            if case_id in seen_ids:
                errors.append(
                    _suite_error(
                        case_index=index,
                        case_id=case_id,
                        path="id",
                        code="duplicate_id",
                        message=f"用例 id 与 cases[{seen_ids[case_id]}] 重复",
                    )
                )
            else:
                seen_ids[case_id] = index
        valid_cases.append(case)
    return valid_cases, errors


def _public_run(run: dict[str, Any]) -> dict[str, Any]:
    evidence = [
        {
            **item,
            "url": f"/api/runs/{run['run_id']}/evidence/{item['path']}",
        }
        for item in run["evidence"]
    ]
    return {
        "run_id": run["run_id"],
        "status": run["status"],
        "created_at": run["created_at"],
        "started_at": run["started_at"],
        "finished_at": run["finished_at"],
        "cases_total": run["cases_total"],
        "cases_done": run["cases_done"],
        "summary": run["summary"],
        "first_failure": run["first_failure"],
        "cases": run["cases"],
        "evidence": evidence,
    }


def create_app(*, config_path: str | None = None) -> FastAPI:
    username = os.environ.get("MTP_WEB_USERNAME", "").strip()
    password = os.environ.get("MTP_WEB_PASSWORD", "")
    session_secret = os.environ.get("MTP_SESSION_SECRET", "")
    if not username or not password or not session_secret:
        raise RuntimeError(
            "启动 Web 服务前必须设置 MTP_WEB_USERNAME、MTP_WEB_PASSWORD、MTP_SESSION_SECRET"
        )

    config_errors: list[str] = []
    try:
        config = load_config(config_path)
        artifacts_root = config.artifact_root().resolve()
    except Exception as exc:  # noqa: BLE001 - expose deployment errors through HTTP
        config_errors.append(f"{type(exc).__name__}: {exc}")
        artifacts_root = Path(os.environ.get("MTP_ARTIFACT_ROOT", "artifacts")).resolve()
    try:
        max_workers = _positive_int("MTP_MAX_CONCURRENT_RUNS", 1)
    except RuntimeError as exc:
        config_errors.append(str(exc))
        max_workers = 1
    try:
        max_upload_bytes = _positive_int("MTP_MAX_UPLOAD_BYTES", 2 * 1024 * 1024)
    except RuntimeError as exc:
        config_errors.append(str(exc))
        max_upload_bytes = 2 * 1024 * 1024
    config_error = "; ".join(config_errors) or None
    artifacts_root.mkdir(parents=True, exist_ok=True)
    repository = RunRepository(artifacts_root / "mtp-platform.sqlite3")
    manager = JobManager(
        repository=repository,
        artifacts_root=artifacts_root,
        config_path=config_path,
        max_workers=max_workers,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        manager.start()
        try:
            yield
        finally:
            manager.stop()

    app = FastAPI(
        title="mtp-platform",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=session_secret,
        session_cookie="mtp_session",
        same_site="lax",
        https_only=os.environ.get("MTP_COOKIE_SECURE", "").lower()
        in {"1", "true", "yes"},
        max_age=8 * 60 * 60,
    )
    app.mount("/static", StaticFiles(directory=str(WEB_ROOT / "static")), name="static")
    app.state.repository = repository
    app.state.job_manager = manager
    app.state.artifacts_root = artifacts_root
    app.state.config_error = config_error

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if request.session.get("username"):
            return RedirectResponse("/", status_code=303)
        return templates.TemplateResponse(
            request=request,
            name="login.html",
            context={"csrf_token": _csrf_token(request), "error": None},
        )

    @app.post("/login", response_class=HTMLResponse)
    def login(
        request: Request,
        submitted_username: Annotated[str, Form(alias="username")],
        submitted_password: Annotated[str, Form(alias="password")],
        csrf_token: Annotated[str, Form()],
    ):
        _verify_csrf(request, csrf_token)
        valid = secrets.compare_digest(
            submitted_username, username
        ) & secrets.compare_digest(submitted_password, password)
        if not valid:
            return templates.TemplateResponse(
                request=request,
                name="login.html",
                context={
                    "csrf_token": _csrf_token(request),
                    "error": "用户名或密码错误",
                },
                status_code=401,
            )
        request.session.clear()
        request.session["username"] = username
        request.session["csrf_token"] = secrets.token_urlsafe(32)
        return RedirectResponse("/", status_code=303)

    @app.post("/logout")
    def logout(
        request: Request,
        csrf_token: Annotated[str, Form()],
        _user: str = Depends(_require_user),
    ):
        _verify_csrf(request, csrf_token)
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.get("/", response_class=HTMLResponse)
    def run_list(request: Request, _user: str = Depends(_require_user)):
        return templates.TemplateResponse(
            request=request,
            name="runs.html",
            context={
                "runs": repository.list(),
                "csrf_token": _csrf_token(request),
                "username": _user,
            },
        )

    @app.get("/runs/new", response_class=HTMLResponse)
    def run_new(request: Request, _user: str = Depends(_require_user)):
        return templates.TemplateResponse(
            request=request,
            name="run_new.html",
            context={"csrf_token": _csrf_token(request), "username": _user},
        )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_detail(request: Request, run_id: str, _user: str = Depends(_require_user)):
        run = repository.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        return templates.TemplateResponse(
            request=request,
            name="run_detail.html",
            context={
                "run": _public_run(run),
                "csrf_token": _csrf_token(request),
                "username": _user,
            },
        )

    @app.get("/api/runs")
    def api_runs(_user: str = Depends(_require_user)) -> list[dict[str, Any]]:
        return [_public_run(item) for item in repository.list()]

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str, _user: str = Depends(_require_user)) -> dict[str, Any]:
        run = repository.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _public_run(run)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        request: Request,
        suite: Annotated[list[UploadFile], File()],
        csrf_token: Annotated[str, Form()],
        allow_write: Annotated[bool, Form()] = False,
        _user: str = Depends(_require_user),
    ) -> dict[str, Any]:
        _verify_csrf(request, csrf_token)
        if config_error:
            raise HTTPException(
                status_code=503,
                detail={"message": "服务配置无效，无法创建任务", "error": config_error},
            )
        if len(suite) != 1:
            for upload in suite:
                await upload.close()
            raise HTTPException(
                status_code=400,
                detail={"message": "一次必须上传一个 JSON 测试套件文件", "errors": []},
            )
        upload = suite[0]
        run_id = new_run_id()
        upload_root = (artifacts_root / "uploads" / run_id).resolve()
        upload_root.mkdir(parents=True, exist_ok=False)
        try:
            supplied_name = upload.filename or ""
            original_name = Path(supplied_name).name
            unsafe_name = (
                not supplied_name
                or supplied_name != original_name
                or Path(supplied_name).is_absolute()
                or ".." in Path(supplied_name).parts
            )
            if unsafe_name or original_name != SUITE_FILENAME:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "只允许上传一个 test-suite.json 文件", "errors": []},
                )

            target = upload_root / "suite.json"
            size = 0
            with target.open("xb") as output:
                while chunk := await upload.read(64 * 1024):
                    size += len(chunk)
                    if size > max_upload_bytes:
                        raise HTTPException(
                            status_code=413,
                            detail=f"测试套件超过 {max_upload_bytes} 字节",
                        )
                    output.write(chunk)
            try:
                payload = json.loads(target.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                raise HTTPException(
                    status_code=422,
                    detail={
                        "message": "测试套件不是合法 JSON",
                        "errors": [
                            _suite_error(
                                case_index=None,
                                case_id=None,
                                path="",
                                code="invalid_json",
                                message=str(exc),
                            )
                        ],
                    },
                ) from exc
            cases, validation_errors = _validate_suite(payload)
            if validation_errors:
                raise HTTPException(
                    status_code=422,
                    detail={"message": "测试套件校验失败", "errors": validation_errors},
                )
            valid_paths: list[Path] = []
            for index, case in enumerate(cases, start=1):
                case_path = upload_root / f"case-{index:03d}.json"
                case_path.write_text(
                    json.dumps(case, ensure_ascii=False, separators=(",", ":")),
                    encoding="utf-8",
                )
                valid_paths.append(case_path)
        except Exception:
            shutil.rmtree(upload_root, ignore_errors=True)
            raise
        finally:
            await upload.close()

        manager.submit(
            run_id=run_id,
            uploads=valid_paths,
            allow_write=allow_write,
        )
        return {
            "run_id": run_id,
            "status": "queued",
            "url": f"/runs/{run_id}",
        }

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(
        request: Request,
        run_id: str,
        csrf_token: Annotated[str | None, Form()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
        _user: str = Depends(_require_user),
    ) -> dict[str, Any]:
        _verify_csrf(request, csrf_token or x_csrf_token)
        if not repository.get(run_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        if not manager.cancel(run_id):
            raise HTTPException(status_code=409, detail="任务已经结束，无法取消")
        return {"run_id": run_id, "cancel_requested": True}

    @app.get("/api/runs/{run_id}/evidence/{evidence_path:path}")
    def download_evidence(
        run_id: str, evidence_path: str, _user: str = Depends(_require_user)
    ):
        run = repository.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        entry = next((item for item in run["evidence"] if item["path"] == evidence_path), None)
        if not entry:
            raise HTTPException(status_code=404, detail="证据不存在")
        run_root = artifacts_root / "runs" / run_id
        target = _safe_child(run_root, evidence_path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="证据不存在")
        return FileResponse(target, filename=target.name, media_type=entry["mime_type"])

    @app.exception_handler(401)
    async def unauthorized(request: Request, exc: HTTPException):
        if not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(status_code=401, content={"detail": exc.detail})

    return app
