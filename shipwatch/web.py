from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shipwatch.config import Settings, load_cookie_file, load_settings
from shipwatch.db import Database
from shipwatch.pipeline import Pipeline


BASE_DIR = Path(__file__).resolve().parent


def _status_label(last_error: str | None, result_count: int | None) -> str:
    if last_error and result_count:
        return "发现成功 / 正文获取失败"
    if last_error:
        return "失败"
    return "成功"


def _failure_reason(value: str | None) -> str | None:
    if not value:
        return None
    labels = []
    for keyword, label in (
        ("antispider", "搜狗反爬"),
        ("反爬", "搜狗反爬"),
        ("appmsgcaptcha", "微信验证码"),
        ("验证码", "微信/搜狗验证码"),
        ("未跳转到微信原文", "原文跳转失败"),
        ("正文过短", "正文为空或过短"),
        ("SSL", "官网 TLS/SSL 连接失败"),
        ("待补采", "正文尚未成功获取"),
    ):
        if keyword.lower() in value.lower() and label not in labels:
            labels.append(label)
    return "；".join(labels) if labels else value[:300]


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = Database(settings.db_path)
    db.init()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        db.init()
        yield

    app = FastAPI(title=settings.web_title, lifespan=lifespan)
    app.state.settings = settings
    app.state.db = db
    templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    def base_context(request: Request, active: str) -> dict:
        return {
            "request": request,
            "title": settings.web_title,
            "active": active,
        }

    def wechat_cookie_path() -> Path:
        configured = os.getenv("SHIPWATCH_WECHAT_COOKIE_FILE")
        if configured:
            return Path(configured)
        return settings.db_path.parent / "wechat_cookies.txt"

    def reload_wechat_cookie() -> None:
        settings.wechat_cookie = os.getenv("SHIPWATCH_WECHAT_COOKIE") or load_cookie_file(
            wechat_cookie_path()
        )

    async def form_data(request: Request) -> dict[str, str]:
        raw = (await request.body()).decode("utf-8")
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[-1] if values else "" for key, values in parsed.items()}

    def redirect_to_wechat_session(**params: object) -> RedirectResponse:
        query = urlencode({key: value for key, value in params.items() if value is not None})
        return RedirectResponse(
            f"/wechat-session?{query}" if query else "/wechat-session",
            status_code=303,
        )

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        yard: str = "",
        progress: str = "",
        review_status: str = "",
        q: str = "",
    ):
        projects = db.project_rows(yard or None, progress or None, review_status or None, q or None)
        metrics = {
            "projects": db.scalar("SELECT COUNT(*) FROM projects") or 0,
            "starts": db.scalar("SELECT COUNT(*) FROM projects WHERE current_progress='开工'") or 0,
            "reviews": db.scalar("SELECT COUNT(*) FROM projects WHERE review_status='待复核'") or 0,
            "source_errors": db.scalar(
                "SELECT COUNT(*) FROM crawl_state WHERE last_error IS NOT NULL"
            ) or 0,
        }
        context = base_context(request, "projects")
        context.update(
            {
                "projects": projects,
                "metrics": metrics,
                "yards": [row[0] for row in db.query("SELECT DISTINCT yard FROM projects ORDER BY yard")],
                "progresses": [
                    row[0]
                    for row in db.query(
                        """
                        SELECT DISTINCT current_progress FROM projects
                        WHERE current_progress IS NOT NULL AND current_progress != ''
                        ORDER BY 1
                        """
                    )
                ],
                "filters": {
                    "yard": yard,
                    "progress": progress,
                    "review_status": review_status,
                    "q": q,
                },
                "export_query": urlencode(
                    {k: v for k, v in {
                        "yard": yard, "progress": progress,
                        "review_status": review_status, "q": q,
                    }.items() if v}
                ),
            }
        )
        return templates.TemplateResponse(request, "dashboard.html", context)

    @app.get("/milestones", response_class=HTMLResponse)
    def milestones(request: Request):
        rows = db.query(
            """
            SELECT p.yard, p.owner_project, p.ship_type, m.label, m.event_date,
                   m.is_expected, m.evidence, a.title, a.url
            FROM milestones m JOIN projects p ON p.id=m.project_id
            JOIN articles a ON a.id=m.article_id
            ORDER BY COALESCE(m.event_date, a.published_at) DESC, m.id DESC
            """
        )
        context = base_context(request, "milestones")
        context["rows"] = rows
        return templates.TemplateResponse(request, "milestones.html", context)

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request, status: str = ""):
        sql = "SELECT * FROM articles"
        params: tuple = ()
        if status:
            sql += " WHERE fetch_status=?"
            params = (status,)
        sql += " ORDER BY fetched_at DESC LIMIT 1000"
        context = base_context(request, "sources")
        context.update({"rows": db.query(sql, params), "status": status})
        return templates.TemplateResponse(request, "sources.html", context)

    @app.get("/source-status", response_class=HTMLResponse)
    def source_status(request: Request):
        states = {row["source_key"]: row for row in db.query("SELECT * FROM crawl_state")}
        rows = []
        for source in settings.sources:
            channels = []
            if source.website:
                channels.append(("website", "官网"))
            if source.wechat:
                channels.append(("wechat", "微信公众号"))
            for channel_key, channel_name in channels:
                state = states.get(f"{source.id}:{channel_key}")
                if state:
                    rows.append(
                        {
                            "yard": source.yard,
                            "channel": channel_name,
                            "status": _status_label(state["last_error"], state["result_count"]),
                            "last_attempt_at": state["last_attempt_at"],
                            "last_success_at": state["last_success_at"],
                            "result_count": state["result_count"],
                            "error": _failure_reason(state["last_error"]),
                        }
                    )
                else:
                    rows.append(
                        {
                            "yard": source.yard,
                            "channel": channel_name,
                            "status": "未执行",
                            "last_attempt_at": None,
                            "last_success_at": None,
                            "result_count": None,
                            "error": "尚无采集记录",
                        }
                    )
        context = base_context(request, "source_status")
        context["rows"] = rows
        return templates.TemplateResponse(request, "source_status.html", context)

    @app.get("/wechat-session", response_class=HTMLResponse)
    def wechat_session(request: Request, msg: str = "", error: str = ""):
        reload_wechat_cookie()
        cookie_path = wechat_cookie_path()
        context = base_context(request, "wechat_session")
        context.update(
            {
                "cookie_configured": bool(settings.wechat_cookie),
                "cookie_path": str(cookie_path),
                "cookie_updated_at": datetime.fromtimestamp(cookie_path.stat().st_mtime).isoformat()
                if cookie_path.exists()
                else None,
                "sources": [source for source in settings.sources if source.wechat],
                "msg": msg,
                "error": error,
            }
        )
        return templates.TemplateResponse(request, "wechat_session.html", context)

    @app.post("/wechat-session/save")
    async def save_wechat_session(request: Request):
        data = await form_data(request)
        cookie = data.get("cookie", "").strip()
        if not cookie:
            return redirect_to_wechat_session(error="Cookie 内容为空，未保存")
        cookie_path = wechat_cookie_path()
        cookie_path.parent.mkdir(parents=True, exist_ok=True)
        cookie_path.write_text(cookie + "\n", encoding="utf-8")
        reload_wechat_cookie()
        return redirect_to_wechat_session(msg="微信 Cookie 已保存")

    @app.post("/wechat-session/refetch")
    async def refetch_wechat_session(request: Request):
        reload_wechat_cookie()
        if not settings.wechat_cookie:
            return redirect_to_wechat_session(error="尚未配置微信 Cookie，不能补采")
        data = await form_data(request)
        source_id = data.get("source") or None
        try:
            limit = max(1, min(int(data.get("limit", "50")), 500))
        except ValueError:
            limit = 50
        pipeline = Pipeline(settings)
        result = pipeline.refetch_wechat(source_id=source_id, limit=limit)
        extract_result = None
        if data.get("extract_after") == "1":
            extract_result = pipeline.extract_pending()
        message = (
            f"补采完成：选中 {result['selected']}，成功 {result['ok']}，"
            f"部分 {result['partial']}，失败 {result['failed']}"
        )
        if extract_result:
            message += (
                f"；抽取 {extract_result['processed']}，相关 {extract_result['relevant']}"
            )
        return redirect_to_wechat_session(msg=message)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "projects": db.scalar("SELECT COUNT(*) FROM projects") or 0,
            "articles": db.scalar("SELECT COUNT(*) FROM articles") or 0,
        }

    return app


app = create_app()
