from __future__ import annotations

import os
import hashlib
import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from shipwatch.config import Settings, SourceConfig, load_settings, single_wechat_account
from shipwatch.db import Database
from shipwatch.pipeline import Pipeline
from shipwatch.text import normalize_url

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
        ("待补采", "正文尚未成功获取"),
    ):
        if keyword.lower() in value.lower() and label not in labels:
            labels.append(label)
    return "；".join(labels) if labels else value[:300]


def _generated_source_id(yard: str, account: str, existing_ids: set[str]) -> str:
    basis = f"{yard}|{account}".strip("|") or "source"
    ascii_slug = re.sub(r"[^a-z0-9_]+", "_", basis.lower()).strip("_")
    if not ascii_slug or not re.search(r"[a-z]", ascii_slug):
        digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]
        ascii_slug = f"custom_{digest}"
    candidate = ascii_slug[:48]
    if candidate and candidate[0].isdigit():
        candidate = f"src_{candidate}"
    original = candidate
    suffix = 2
    while candidate in existing_ids:
        tail = f"_{suffix}"
        candidate = f"{original[:48 - len(tail)]}{tail}"
        suffix += 1
    return candidate


def _progress_options(db: Database, yard: str = "", review_status: str = "", q: str = "") -> list[str]:
    conditions = ["p.current_progress='开工'"]
    params: list[object] = []
    if yard:
        conditions.append("p.yard=?")
        params.append(yard)
    if review_status:
        conditions.append("p.review_status=?")
        params.append(review_status)
    if q:
        conditions.append(
            """
            (COALESCE(p.owner_project, '') LIKE ? OR COALESCE(p.ship_type, '') LIKE ?
             OR COALESCE(p.series_identifier, '') LIKE ? OR COALESCE(a.title, '') LIKE ?)
            """
        )
        params.extend([f"%{q}%"] * 4)
    rows = db.query(
        f"""
        SELECT DISTINCT COALESCE(p.current_progress, '') AS progress
        FROM projects p
        JOIN project_sources ps ON ps.project_id=p.id
        JOIN articles a ON a.id=ps.article_id AND a.channel='微信公众号'
        WHERE ps.article_id=(
          SELECT MAX(ps2.article_id)
          FROM project_sources ps2
          JOIN articles a2 ON a2.id=ps2.article_id
          WHERE ps2.project_id=p.id AND a2.channel='微信公众号'
        ) AND {" AND ".join(conditions)}
        ORDER BY
          CASE p.current_progress
            WHEN '签约/立项' THEN 1 WHEN '签约' THEN 2 WHEN '开工' THEN 3
            WHEN '铺龙骨' THEN 4 WHEN '开工|铺龙骨' THEN 5
            WHEN '下水/出坞' THEN 6 WHEN '命名' THEN 7 WHEN '试航' THEN 8
            WHEN '交付/完工' THEN 9 ELSE 10
          END,
          progress
        """,
        tuple(params),
    )
    return [row["progress"] for row in rows]


def _active_source_channels(settings: Settings) -> list[tuple[str, SourceConfig, str, str]]:
    rows = []
    for source in settings.sources:
        if source.wechat:
            rows.append((f"{source.id}:wechat", source, "wechat", "微信公众号"))
    return rows


def _source_error_count(db: Database, settings: Settings) -> int:
    source_keys = [source_key for source_key, *_ in _active_source_channels(settings)]
    if not source_keys:
        return 0
    placeholders = ",".join("?" for _ in source_keys)
    return db.scalar(
        f"""
        SELECT COUNT(*) FROM crawl_state
        WHERE last_error IS NOT NULL AND source_key IN ({placeholders})
        """,
        tuple(source_keys),
    ) or 0


def _project_count(
    db: Database,
    review_status: str | None = None,
    start_only: bool = False,
    wechat_only: bool = False,
    exclude_irrelevant: bool = False,
) -> int:
    params: tuple[object, ...] = ()
    status_filter = ""
    if review_status:
        status_filter = " AND p.review_status=?"
        params = (review_status,)
    start_filter = " AND p.current_progress='开工'" if start_only else ""
    irrelevant_filter = " AND p.review_status!='无关'" if exclude_irrelevant else ""
    source_filter = """
        AND EXISTS (
          SELECT 1
          FROM project_sources ps
          JOIN articles a ON a.id=ps.article_id
          WHERE ps.project_id=p.id AND a.channel='微信公众号'
        )
    """ if wechat_only else ""
    return db.scalar(
        f"""
        SELECT COUNT(*) FROM projects p
        WHERE 1=1{source_filter}{start_filter}{irrelevant_filter}{status_filter}
        """,
        params,
    ) or 0


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

    async def form_data(request: Request) -> dict:
        return dict(await request.form())

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
            "projects": _project_count(db),
            "start_candidates": _project_count(
                db, start_only=True, wechat_only=True, exclude_irrelevant=True
            ),
            "confirmed": _project_count(db, "已确认"),
            "reviews": _project_count(db, "待复核"),
            "duplicates": _project_count(db, "可能重复"),
            "irrelevant": _project_count(db, "无关"),
            "source_errors": _source_error_count(db, settings),
        }
        context = base_context(request, "projects")
        context.update(
            {
                "projects": projects,
                "metrics": metrics,
                "yards": [
                    row[0]
                    for row in db.query(
                        """
                        SELECT DISTINCT p.yard
                        FROM projects p
                        WHERE EXISTS (
                          SELECT 1
                          FROM project_sources ps
                          JOIN articles a ON a.id=ps.article_id
                          WHERE ps.project_id=p.id AND a.channel='微信公众号'
                        )
                        ORDER BY p.yard
                        """
                    )
                ],
                "progresses": _progress_options(db, yard, review_status, q),
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
            WHERE p.current_progress='开工' AND m.kind='start'
            ORDER BY COALESCE(m.event_date, a.published_at) DESC, m.id DESC
            """
        )
        context = base_context(request, "milestones")
        context["rows"] = rows
        return templates.TemplateResponse(request, "milestones.html", context)

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request, status: str = "", yard_hint: str = ""):
        sql = "SELECT * FROM articles"
        params: list = []
        filters = []
        if status:
            filters.append("fetch_status=?")
            params.append(status)
        if yard_hint:
            filters.append("yard_hint=?")
            params.append(yard_hint)
        if filters:
            sql += " WHERE " + " AND ".join(filters)
        sql += " ORDER BY fetched_at DESC LIMIT 1000"
        context = base_context(request, "sources")
        yards = [row[0] for row in db.query("SELECT DISTINCT yard_hint FROM articles ORDER BY yard_hint")]
        ok_urls = {
            row["url"]
            for row in db.query(
                """
                SELECT url FROM articles
                WHERE fetch_status='ok' AND url LIKE 'https://mp.weixin.qq.com/s/%'
                """
            )
        }
        rows = []
        for row in db.query(sql, tuple(params)):
            item = dict(row)
            item["open_url"] = normalize_url(item["url"])
            if item["url"] != item["open_url"] and item["open_url"] in ok_urls:
                continue
            rows.append(item)
        context.update({"rows": rows, "status": status, "yard_hint": yard_hint, "yards": yards})
        return templates.TemplateResponse(request, "sources.html", context)

    @app.get("/source-status", response_class=HTMLResponse)
    def source_status(request: Request):
        states = {row["source_key"]: row for row in db.query("SELECT * FROM crawl_state")}
        rows = []
        for source_key, source, _channel_key, channel_name in _active_source_channels(settings):
            state = states.get(source_key)
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

    @app.get("/api-usage", response_class=HTMLResponse)
    def api_usage(request: Request):
        context = base_context(request, "api_usage")
        context["totals"] = db.api_usage_totals()
        context["rows"] = db.api_usage_summary(days=7)
        return templates.TemplateResponse(request, "api_usage.html", context)


    @app.get("/source-config", response_class=HTMLResponse)
    def source_config(request: Request):
        import json
        override_path = Path(os.environ.get("SHIPWATCH_OVERRIDES_FILE", "data/source_overrides.json"))
        overrides = {}
        if override_path.exists():
            overrides = json.loads(override_path.read_text(encoding="utf-8"))
        items = []
        custom_ids = {item.get("id") for item in overrides.get("__custom__", [])}
        for src in settings.sources:
            if src.id in custom_ids:
                continue
            o = overrides.get(src.id, {})
            accounts = single_wechat_account(o.get("wechat_accounts")) or (
                single_wechat_account(src.wechat.account_names) if src.wechat else ""
            )
            items.append({"id": src.id, "yard": src.yard, "accounts": accounts, "enabled": not o.get("disabled", False)})
        context = base_context(request, "source_config")
        custom_items = overrides.get("__custom__", [])
        for ci in custom_items:
            ci["accounts"] = single_wechat_account(ci.get("wechat_accounts"))
        context["items"] = items
        context["custom_items"] = custom_items
        return templates.TemplateResponse(request, "source_config.html", context)

    @app.post("/source-config/save")
    async def source_config_save(request: Request):
        import json
        form = await request.form()
        override_path = Path(os.environ.get("SHIPWATCH_OVERRIDES_FILE", "data/source_overrides.json"))
        overrides = json.loads(override_path.read_text(encoding="utf-8")) if override_path.exists() else {}
        # Save custom sources
        custom_list = []
        new_account = single_wechat_account(form.get("new_accounts", ""))
        new_yard = form.get("new_yard", "").strip() or new_account
        if new_account:
            existing_ids = {src.id for src in settings.sources}
            existing_ids.update(item.get("id", "") for item in overrides.get("__custom__", []))
            nid = _generated_source_id(new_yard, new_account, existing_ids)
            account = single_wechat_account(form.get("new_accounts", ""))
            custom_list.append(
                {
                    "id": nid,
                    "yard": new_yard,
                    "wechat_accounts": [account] if account else [],
                    "enabled": form.get("cenabled_new") == "1",
                }
            )
        # Keep existing customs
        for ec in overrides.get("__custom__", []):
            ec_id = ec["id"]
            ec["enabled"] = form.get("cenabled_" + ec_id) == "1"
            account = single_wechat_account(form.get("caccounts_" + ec_id, ""))
            if account:
                ec["wechat_accounts"] = [account]
            custom_list.append(ec)
        overrides["__custom__"] = custom_list

        for src in settings.sources:
            o = overrides.get(src.id, {})
            o["disabled"] = form.get("enabled_" + src.id) != "1"
            account = single_wechat_account(form.get("accounts_" + src.id, ""))
            if account:
                o["wechat_accounts"] = [account]
            elif "wechat_accounts" in o:
                del o["wechat_accounts"]
            overrides[src.id] = o
        override_path.parent.mkdir(parents=True, exist_ok=True)
        override_path.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/source-config", status_code=303)

    @app.get("/article/{article_id}", response_class=HTMLResponse)
    def article_detail(request: Request, article_id: int):
        row = db.query("SELECT * FROM articles WHERE id=?", (article_id,))
        if not row:
            return HTMLResponse("文章不存在", status_code=404)
        context = base_context(request, "sources")
        article = dict(row[0])
        article["open_url"] = normalize_url(article["url"])
        context["article"] = article
        return templates.TemplateResponse(request, "article.html", context)

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "projects": db.scalar("SELECT COUNT(*) FROM projects") or 0,
            "articles": db.scalar("SELECT COUNT(*) FROM articles") or 0,
        }

    return app

app = create_app()
