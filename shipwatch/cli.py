from __future__ import annotations

import argparse
import logging
from datetime import date
from pathlib import Path

from shipwatch.config import load_settings
from shipwatch.pipeline import Pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="五家船厂新船项目采集系统")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init", help="初始化数据库")

    collect = sub.add_parser("collect", help="发现并采集文章")
    collect.add_argument("--since", help="起始日期 YYYY-MM-DD")
    collect.add_argument("--source", help="只处理指定来源ID，多个用逗号隔开")
    collect.add_argument("--website-only", action="store_true", help="兼容旧参数；官网采集已关闭")
    collect.add_argument("--wechat-only", action="store_true", help="兼容旧参数；当前默认仅采集公众号")
    add = sub.add_parser("add-url", help="人工补充微信公众号原文链接")
    add.add_argument("url")
    add.add_argument("--source", required=True, help="来源ID，例如 hudong_zhonghua")
    add.add_argument("--title", default="人工补充链接")

    sub.add_parser("extract", help="抽取待处理文章并合并项目")
    export = sub.add_parser("export", help="生成 Excel")
    export.add_argument("--output", type=Path)
    sub.add_parser("daily", help="执行每日采集、抽取与导出")
    web = sub.add_parser("web", help="启动网页看板")
    web.add_argument("--host")
    web.add_argument("--port", type=int)
    sub.add_parser("stats", help="显示数据统计")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = load_settings(args.config)
    pipeline = Pipeline(settings)

    if args.command == "init":
        print(f"数据库已初始化：{settings.db_path}")
    elif args.command == "collect":
        since = date.fromisoformat(args.since) if args.since else None
        result = pipeline.discover_and_collect(
            since=since,
            websites=False,
            wechat=True,
            source_ids=args.source.split(",") if args.source else None,
        )
        print(result)
    elif args.command == "add-url":
        article_id = pipeline.add_url(args.url, args.source, args.title)
        print({"article_id": article_id})
    elif args.command == "extract":
        print(pipeline.extract_pending())
    elif args.command == "export":
        print(pipeline.export(args.output))
    elif args.command == "daily":
        collected, extracted = pipeline.run_daily()
        print({"collect": collected, "extract": extracted})
    elif args.command == "web":
        import uvicorn

        uvicorn.run(
            "shipwatch.web:app",
            host=args.host or settings.web_host,
            port=args.port or settings.web_port,
        )
    elif args.command == "stats":
        print(
            {
                "articles": pipeline.db.scalar("SELECT COUNT(*) FROM articles"),
                "projects": pipeline.db.scalar("SELECT COUNT(*) FROM projects"),
                "pending_review": pipeline.db.scalar(
                    "SELECT COUNT(*) FROM projects WHERE review_status='待复核'"
                ),
            }
        )


if __name__ == "__main__":
    main()
