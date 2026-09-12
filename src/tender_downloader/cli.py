from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from .classify import build_classifier, LLMClassifier, RuleClassifier
from .query_review import QueryReviewer
from .config import effective_user_agent, load_config, sample_config, validate_config_data
from .database import Database
from .export import (
    export_artifact_refs,
    export_coverage,
    export_current_results,
    export_download_links,
    export_manifest,
    export_notice_aliases,
    export_provenance,
    export_verified_manifest,
    verify_files,
)
from .http_client import HttpClient
from .pipeline import Pipeline
from .sources import build_sources
from .storage import ImmutableStore


def _client(config, *, for_ai: bool = False) -> HttpClient:
    http = config.data.get("http", {})
    ai = config.data.get("ai", {})
    user_agent = effective_user_agent(http)
    return HttpClient(
        user_agent=user_agent,
        timeout_seconds=float(
            ai.get("timeout_seconds", 60) if for_ai else http.get("timeout_seconds", 35)
        ),
        delay_seconds=0 if for_ai else float(http.get("delay_seconds", 3)),
        max_retries=(0 if for_ai and config.data.get("_query", {}).get("criteria", {}).get("mode") == "ai_recall"
                     else int(http.get("max_retries", 2))),
        max_response_bytes=int(float(http.get("max_response_mb", 25)) * 1024 * 1024),
        max_download_bytes=int(float(http.get("max_download_mb", 1024)) * 1024 * 1024),
        download_timeout_seconds=float(http.get("download_timeout_seconds", 900)),
        allow_private_hosts=bool(http.get("allow_private_hosts", False)),
        allow_benchmark_proxy_hosts=bool(
            http.get("allow_benchmark_proxy_hosts", False)
        ),
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if getattr(args, "query_json", None):
        from .query import query_config
        specification = json.loads(args.query_json)
        config = query_config(config, specification["criteria"], specification["id"])
        print("正在按本次条件查询各平台：" + json.dumps(config.data["_query"], ensure_ascii=False), flush=True)
    if getattr(args, "sample", False):
        config = sample_config(config)
        print("开始试采：吉林公共资源采购公告最多10条，不使用AI，不下载附件，不修改原采集配置。", flush=True)
    validate_config_data(config.data, require_ready=True)
    config.output_dir.mkdir(parents=True, exist_ok=True)
    client = _client(config)
    ai_client: HttpClient | None = None
    db: Database | None = None
    try:
        ai = config.data.get("ai", {})
        ai_client = _client(config, for_ai=True) if ai.get("enabled", False) else None
        ai_recall = config.data.get("_query", {}).get("criteria", {}).get("mode") == "ai_recall"
        reviewer = (QueryReviewer(LLMClassifier(ai_client, ai), config.data["_query"]["criteria"])
                    if ai_recall else None)
        db = Database(config.database_path)
        pipeline = Pipeline(
            config=config,
            client=client,
            db=db,
            store=ImmutableStore(config.output_dir),
            classifier=RuleClassifier() if ai_recall else build_classifier(ai_client or client, ai),
            sources=build_sources(client, config.data["sources"], config.path.parent),
            query_reviewer=reviewer,
        )
        summary = pipeline.run()
        print("运行完成：" + "，".join(f"{key}={value}" for key, value in summary.items()))
        print(f"原件和结果目录：{config.output_dir}")
        complete = (
            summary["failed"] == 0
            and summary["partial_sources"] == 0
            and summary["source_failures"] == 0
            and summary.get("unresolved", 0) == 0
        )
        return 0 if complete else 2
    finally:
        if db is not None:
            db.close()
        if ai_client is not None:
            ai_client.close()
        client.close()


def cmd_verify(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    try:
        checked, errors = verify_files(db)
    finally:
        db.close()
    print(f"已校验 {checked} 个文件路径及官方来源门禁")
    for error in errors:
        print(f"错误：{error}")
    return 1 if errors else 0


def cmd_links(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    db = Database(config.database_path)
    try:
        path = export_download_links(db, config.output_dir)
        print(f"下载链接表：{path}")
    finally:
        db.close()
    return 0


def cmd_download(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    client = _client(config)
    db = Database(config.database_path)
    sources = build_sources(client, config.data["sources"], config.path.parent)
    try:
        ids = list(args.notice_id or [])
        if args.ids_file:
            with Path(args.ids_file).open(encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    identity = row.get("标讯ID") or row.get("notice_id") or row.get("identity")
                    if identity:
                        ids.append(identity)
        if args.all:
            page = 1
            while True:
                result = db.list_notices(page=page, page_size=200, query=args.query or "")
                ids.extend(item["notice_id"] for item in result["items"] if item["status"] != "alias")
                if page >= result["pages"]:
                    break
                page += 1
        ids = list(dict.fromkeys(ids))
        if not ids:
            print("没有选中标讯；先运行采集，再指定 --notice-id、--ids-file 或 --all。")
            export_download_links(db, config.output_dir)
            return 2
        pipeline = Pipeline(config=config, client=client, db=db,
                            store=ImmutableStore(config.output_dir),
                            classifier=build_classifier(client, {"enabled": False}), sources=sources)
        results = pipeline.download_notices(ids, include_notice=not args.attachments_only)
        config.output_dir.mkdir(parents=True, exist_ok=True)
        report = config.output_dir / "download_results.json"
        report.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        counts = {state: sum(item["status"] == state for item in results)
                  for state in ("downloaded", "partial", "failed")}
        print("下载结果：" + json.dumps(counts, ensure_ascii=False))
        print(f"下载链接表：{config.output_dir / 'download_links.csv'}")
        return 0 if not counts["partial"] and not counts["failed"] else 2
    finally:
        for source in sources:
            source.close()
        db.close()
        client.close()


def cmd_sources(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    print("已配置来源：")
    for source in config.data["sources"]:
        print(
            f"- {source.get('type')}: enabled={source.get('enabled', True)} "
            f"authority_rank={source.get('authority_rank', 50)}"
        )
    db = Database(config.database_path)
    try:
        rows = db.latest_coverage_rows()
        if rows:
            print("\n最近覆盖状态：")
            for row in rows:
                print(
                    f"- {row['source']}: {row['status']} pages={row['pages']} "
                    f"notices={row['notices']} downloads={row['downloads_ok']} "
                    f"blocked={row['blocked']} {row['message'] or ''}"
                )
        export_manifest(db, config.output_dir)
        export_verified_manifest(db, config.output_dir)
        export_current_results(db, config.output_dir)
        export_download_links(db, config.output_dir)
        export_provenance(db, config.output_dir)
        export_artifact_refs(db, config.output_dir)
        export_notice_aliases(db, config.output_dir)
        export_coverage(db, config.output_dir)
    finally:
        db.close()
    return 0


def cmd_web(args: argparse.Namespace) -> int:
    # Lazy import keeps the crawler CLI independent from the local dashboard.
    from .webui import serve_config_ui

    serve_config_ui(
        args.config,
        port=args.port,
        open_browser=not args.no_browser,
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="吉林省网络安全公开标书下载与 AI 分类")
    parser.add_argument("--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, function, help_text in (
        ("run", cmd_run, "采集标讯信息并 AI 分类（下载由 delivery.mode 控制）"),
        ("verify", cmd_verify, "重新校验原件哈希与官方来源门禁"),
        ("sources", cmd_sources, "显示来源配置和最近覆盖状态"),
        ("links", cmd_links, "导出附件下载链接和公告入口表，不请求网站"),
    ):
        command = subparsers.add_parser(name, help=help_text)
        command.add_argument("--config", required=True, help="JSON 配置文件路径")
        if name == "run":
            command.add_argument("--query-json", help="本次平台查询条件和查询标识")
            command.add_argument("--sample", action="store_true", help="仅试采吉林公共资源最多10条，不改保存配置")
        command.set_defaults(function=function)
    download = subparsers.add_parser("download", help="批量下载已采集标讯，失败项保留下载链接")
    download.add_argument("--config", required=True)
    selection = download.add_mutually_exclusive_group(required=True)
    selection.add_argument("--notice-id", action="append", help="标讯 ID，可重复指定")
    selection.add_argument("--ids-file", help="含标讯ID列的下载链接 CSV")
    selection.add_argument("--all", action="store_true", help="下载清单中的全部标讯")
    download.add_argument("--query", help="与 --all 配合按标题、采购人、厂商或项目编号筛选")
    download.add_argument("--attachments-only", action="store_true", help="只交付附件")
    download.set_defaults(function=cmd_download)
    web = subparsers.add_parser("web", help="打开本机可视化配置与运行控制台")
    web.add_argument("--config", required=True, help="JSON 配置文件路径")
    web.add_argument("--port", type=int, default=8765, help="本机监听端口，默认 8765")
    web.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    web.set_defaults(function=cmd_web)
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )
    try:
        code = int(args.function(args))
    except KeyboardInterrupt:
        print("已由用户中止，断点文件会保留供下次续传。", file=sys.stderr)
        code = 130
    except Exception as exc:
        logging.exception("运行失败：%s", exc)
        code = 1
    raise SystemExit(code)
