from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shipwatch.config import Settings, load_settings
from shipwatch.db import Database


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
            for channel_key, channel_name in (("website", "官网"), ("wechat", "微信公众号")):
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

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "projects": db.scalar("SELECT COUNT(*) FROM projects") or 0,
            "articles": db.scalar("SELECT COUNT(*) FROM articles") or 0,
        }

    return app


app = create_app()
