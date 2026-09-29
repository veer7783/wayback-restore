#!/usr/bin/env python3
"""Project Hindu Kush Wayback restorer CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pipeline.batch10 import run_batch10
from pipeline.batch40 import run_batch40
from pipeline.static_pages import run_static_pages
from pipeline.context import build_runtime
from pipeline.discover import run_discover
from pipeline.report import run_report
from pipeline.restore import prepare_new_imports, run_extract, run_media, run_restore, run_scrape
from pipeline.setup import inspect_listingpro, install_listingpro_stack, install_theme, run_setup
from pipeline.wp_status import run_wp_status
from pipeline.snapshots_cmd import run_snapshots
from pipeline.validate import run_validate


def _limit(args: argparse.Namespace, default: int) -> int:
    return int(args.limit if args.limit is not None else default)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Restore projecthindukush.com from the Wayback Machine")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("setup", help="Verify XAMPP WordPress and install the restoration plugin")
    sub.add_parser("wp-status", help="Verify local WordPress, database, REST, and admin")
    sub.add_parser("install-listingpro", help="Install supplied ListingPro 2.9.12 and bundled required plugins")

    discover = sub.add_parser("discover", help="Phase 1: CDX discovery")
    discover.add_argument("--limit", type=int, default=None)

    snapshots = sub.add_parser("snapshots", help="Phase 2: select best snapshots")
    snapshots.add_argument("--limit", type=int, default=None)

    scrape = sub.add_parser("scrape", help="Download selected archived HTML")
    scrape.add_argument("--limit", type=int, default=None)

    extract = sub.add_parser("extract", help="Parse HTML and extract listing fields")
    extract.add_argument("--limit", type=int, default=None)

    media = sub.add_parser("media", help="Download archived listing media")
    media.add_argument("--limit", type=int, default=None)

    sub.add_parser("inspect-listingpro", help="Inspect installed ListingPro schema")

    restore = sub.add_parser(
        "restore",
        help="Import N new unique listings that are not already in WordPress",
    )
    restore.add_argument("--limit", type=int, required=True, help="How many new unique posts to save")
    restore.add_argument("--dry-run", action="store_true")
    restore.add_argument(
        "--no-scrape",
        action="store_true",
        help="Use existing extracted JSON; do not re-run discover/scrape/extract",
    )

    sub.add_parser("batch-10", help="Restore exactly 10 ListingPro pages with media, then stop")
    sub.add_parser("batch-40", help="Restore exactly 40 additional ListingPro pages, then stop")
    sub.add_parser("static-pages", help="Restore archived home and verified static pages")

    validate = sub.add_parser("validate", help="Validate restored WordPress pages")
    validate.add_argument("--limit", type=int, default=None)

    sub.add_parser("retry-failed", help="Retry URLs recorded in the error log")
    sub.add_parser("report", help="Print restoration progress")

    theme = sub.add_parser("install-theme", help="Install a legitimate ListingPro ZIP")
    theme.add_argument("zip_path")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    runtime = build_runtime()
    default_limit = int(runtime.config["restore"]["default_limit"])

    try:
        if args.command == "setup":
            run_setup(runtime)
        elif args.command == "wp-status":
            run_wp_status(runtime)
        elif args.command == "install-listingpro":
            report = install_listingpro_stack(runtime)
            print(json.dumps(report, indent=2))
        elif args.command == "discover":
            summary = run_discover(runtime, _limit(args, default_limit))
            print(f"Discovered URLs: {summary['discovered_urls']}")
            print(f"Unique incident URLs: {summary['unique_incident_urls']}")
            print(f"Snapshots available: {summary['snapshots_available']}")
            print(f"Missing snapshots: {summary['missing_snapshots']}")
        elif args.command == "snapshots":
            count = run_snapshots(runtime, _limit(args, default_limit))
            print(f"Selected snapshots: {count}")
        elif args.command == "scrape":
            count = run_scrape(runtime, _limit(args, default_limit))
            print(f"HTML downloaded: {count}")
        elif args.command == "extract":
            count = run_extract(runtime, _limit(args, default_limit))
            print(f"Pages extracted: {count}")
        elif args.command == "media":
            count = run_media(runtime, _limit(args, default_limit))
            print(f"Media downloaded: {count}")
        elif args.command == "inspect-listingpro":
            schema = inspect_listingpro(runtime)
            print(f"Post types: {schema.get('post_types')}")
            print(f"Taxonomies: {schema.get('taxonomies')}")
            print(f"Meta fields: {len(schema.get('meta_fields') or [])}")
        elif args.command == "restore":
            limit = int(args.limit)
            if not args.no_scrape:
                prepared = prepare_new_imports(runtime, limit)
                print(f"Already in WordPress: {prepared['already']}")
                print(f"New pages selected: {len(prepared['snapshots'])}")
                if not prepared["snapshots"]:
                    print("No new unique incident pages found.")
                    return 0
                run_scrape(runtime, limit)
                run_extract(runtime, limit)
                run_media(runtime, limit)
            result = run_restore(runtime, limit, dry_run=args.dry_run)
            created = [row for row in result["rows"] if row.get("status") == "created"]
            print(f"New posts saved: {len(created)}")
            for row in created:
                print(f"  {row.get('wp_post_id')} {row.get('title')} {row.get('wp_url')}")
            failed = [row for row in result["rows"] if row.get("status") == "failed"]
            if failed:
                print(f"Failed: {len(failed)}")
            if args.dry_run:
                print("Dry run only. WordPress was not modified.")
        elif args.command == "batch-10":
            result = run_batch10(runtime)
            print(json.dumps({k: v for k, v in result.items() if k != "media"}, indent=2, default=str))
            print("HARD STOP after 10 pages.")
        elif args.command == "batch-40":
            result = run_batch40(runtime)
            print(json.dumps(result, indent=2, default=str))
            print("HARD STOP after 40 additional pages.")
        elif args.command == "static-pages":
            result = run_static_pages(runtime)
            print(json.dumps(result, indent=2, default=str))
        elif args.command == "validate":
            rows = run_validate(runtime, _limit(args, default_limit))
            print(f"Validated pages: {len(rows)}")
        elif args.command == "retry-failed":
            errors = runtime.store.failed_urls()
            urls = []
            seen = set()
            for row in errors:
                if row.url in seen:
                    continue
                seen.add(row.url)
                urls.append(row.url)
            print(f"Retrying {len(urls)} failed URLs")
            run_scrape(runtime, max(1, int(runtime.config["restore"]["default_limit"])))
            run_extract(runtime, max(1, int(runtime.config["restore"]["default_limit"])))
            run_media(runtime, max(1, int(runtime.config["restore"]["default_limit"])))
            run_restore(runtime, max(1, int(runtime.config["restore"]["default_limit"])), dry_run=False)
        elif args.command == "report":
            run_report(runtime)
        elif args.command == "install-theme":
            install_theme(runtime, Path(args.zip_path))
        else:
            parser.print_help()
            return 1
    finally:
        runtime.client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
