"""FastAPI application for uploading, running and reviewing test cases."""

from __future__ import annotations

import os
import secrets
import shutil
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
from mtp_contracts.case_validator import load_case, validate_file
from mtp_contracts.results import new_run_id
from starlette.middleware.sessions import SessionMiddleware

from mtp_platform.config import load_config
from mtp_platform.service.jobs import JobManager
from mtp_platform.service.repository import RunRepository

WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(WEB_ROOT / "templates"))
ALLOWED_SUFFIXES = {".yaml", ".yml", ".json"}


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


def _public_run(run: dict[str, Any], artifacts_root: Path) -> dict[str, Any]:
    result = dict(run)
    result["uploads"] = [Path(path).name for path in run["uploads"]]
    result["results"] = [dict(item) for item in run["results"]]
    for item in result["results"]:
        if item.get("source"):
            item["source"] = Path(str(item["source"])).name
    result["report_urls"] = {
        kind: f"/api/runs/{run['run_id']}/reports/{filename}"
        for kind, filename in run["reports"].items()
    }
    run_root = artifacts_root / "runs" / run["run_id"]
    evidence: list[dict[str, str]] = []
    if run_root.exists():
        for path in sorted(run_root.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(run_root)
            if relative.parts[0] in {"reports", "_audit"}:
                continue
            evidence.append(
                {
                    "path": relative.as_posix(),
                    "url": f"/api/runs/{run['run_id']}/evidence/{relative.as_posix()}",
                }
            )
    result["evidence"] = evidence
    return result


def create_app(*, config_path: str | None = None) -> FastAPI:
    username = os.environ.get("MTP_WEB_USERNAME", "").strip()
    password = os.environ.get("MTP_WEB_PASSWORD", "")
    session_secret = os.environ.get("MTP_SESSION_SECRET", "")
    if not username or not password or not session_secret:
        raise RuntimeError(
            "启动 Web 服务前必须设置 MTP_WEB_USERNAME、MTP_WEB_PASSWORD、MTP_SESSION_SECRET"
        )

    config = load_config(config_path)
    artifacts_root = config.artifact_root().resolve()
    artifacts_root.mkdir(parents=True, exist_ok=True)
    repository = RunRepository(artifacts_root / "mtp-platform.sqlite3")
    manager = JobManager(
        repository=repository,
        artifacts_root=artifacts_root,
        config_path=config_path,
        max_workers=_positive_int("MTP_MAX_CONCURRENT_RUNS", 1),
    )
    max_upload_bytes = _positive_int("MTP_MAX_UPLOAD_BYTES", 2 * 1024 * 1024)
    max_upload_files = _positive_int("MTP_MAX_UPLOAD_FILES", 20)

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

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

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
                "run": _public_run(run, artifacts_root),
                "csrf_token": _csrf_token(request),
                "username": _user,
            },
        )

    @app.get("/api/runs")
    def api_runs(_user: str = Depends(_require_user)) -> list[dict[str, Any]]:
        return [_public_run(item, artifacts_root) for item in repository.list()]

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str, _user: str = Depends(_require_user)) -> dict[str, Any]:
        run = repository.get(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="任务不存在")
        return _public_run(run, artifacts_root)

    @app.post("/api/runs", status_code=status.HTTP_202_ACCEPTED)
    async def create_run(
        request: Request,
        files: Annotated[list[UploadFile], File()],
        csrf_token: Annotated[str, Form()],
        office: Annotated[bool, Form()] = False,
        allow_write: Annotated[bool, Form()] = False,
        tag: Annotated[str, Form()] = "",
        _user: str = Depends(_require_user),
    ) -> dict[str, Any]:
        _verify_csrf(request, csrf_token)
        if not files or len(files) > max_upload_files:
            raise HTTPException(
                status_code=400,
                detail=f"一次必须上传 1 至 {max_upload_files} 个文件",
            )

        run_id = new_run_id()
        upload_root = (artifacts_root / "uploads" / run_id).resolve()
        upload_root.mkdir(parents=True, exist_ok=False)
        valid_paths: list[Path] = []
        validation_errors: list[dict[str, Any]] = []
        try:
            for index, upload in enumerate(files, start=1):
                supplied_name = upload.filename or ""
                original_name = Path(supplied_name).name
                suffix = Path(original_name).suffix.lower()
                unsafe_name = (
                    not supplied_name
                    or supplied_name != original_name
                    or Path(supplied_name).is_absolute()
                    or ".." in Path(supplied_name).parts
                )
                if (
                    unsafe_name
                    or suffix not in ALLOWED_SUFFIXES
                    or original_name.lower() == "mtp_config.yaml"
                ):
                    validation_errors.append(
                        {
                            "file": original_name,
                            "messages": ["只允许上传测试用例 YAML/YML/JSON 文件"],
                        }
                    )
                    continue
                target = upload_root / f"case-{index:03d}{suffix}"
                size = 0
                with target.open("xb") as output:
                    while chunk := await upload.read(64 * 1024):
                        size += len(chunk)
                        if size > max_upload_bytes:
                            raise HTTPException(
                                status_code=413,
                                detail=f"文件 {original_name} 超过 {max_upload_bytes} 字节",
                            )
                        output.write(chunk)
                validation = validate_file(target)
                messages = validation.messages()
                if not validation.ok:
                    target.unlink(missing_ok=True)
                    validation_errors.append(
                        {"file": original_name, "messages": messages}
                    )
                    continue
                if tag and tag not in (load_case(target).get("tags") or []):
                    target.unlink(missing_ok=True)
                    validation_errors.append(
                        {"file": original_name, "messages": [f"不包含标签 {tag}"]}
                    )
                    continue
                valid_paths.append(target)
        except Exception:
            shutil.rmtree(upload_root, ignore_errors=True)
            raise
        finally:
            for upload in files:
                await upload.close()

        if not valid_paths:
            shutil.rmtree(upload_root, ignore_errors=True)
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "没有可执行的合法用例",
                    "validation_errors": validation_errors,
                },
            )

        options = {"office": office, "allow_write": allow_write, "tag": tag}
        manager.submit(
            run_id=run_id,
            uploads=valid_paths,
            options=options,
            validation_errors=validation_errors,
        )
        return {
            "run_id": run_id,
            "status": "queued",
            "url": f"/runs/{run_id}",
            "validation_errors": validation_errors,
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

    @app.get("/api/runs/{run_id}/reports/{filename}")
    def download_report(
        run_id: str, filename: str, _user: str = Depends(_require_user)
    ):
        run = repository.get(run_id)
        if not run or filename not in set(run["reports"].values()):
            raise HTTPException(status_code=404, detail="报告不存在")
        target = _safe_child(artifacts_root / "runs" / run_id / "reports", filename)
        if not target.is_file():
            raise HTTPException(status_code=404, detail="报告不存在")
        return FileResponse(target, filename=target.name)

    @app.get("/api/runs/{run_id}/evidence/{evidence_path:path}")
    def download_evidence(
        run_id: str, evidence_path: str, _user: str = Depends(_require_user)
    ):
        if not repository.get(run_id):
            raise HTTPException(status_code=404, detail="任务不存在")
        run_root = artifacts_root / "runs" / run_id
        target = _safe_child(run_root, evidence_path)
        relative = target.relative_to(run_root.resolve())
        if relative.parts[0] in {"reports", "_audit"} or not target.is_file():
            raise HTTPException(status_code=404, detail="证据不存在")
        return FileResponse(target, filename=target.name)

    @app.exception_handler(401)
    async def unauthorized(request: Request, exc: HTTPException):
        if not request.url.path.startswith("/api/"):
            return RedirectResponse("/login", status_code=303)
        return JSONResponse(status_code=401, content={"detail": exc.detail})

    return app
