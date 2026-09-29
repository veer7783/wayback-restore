"""Aggregate restoration progress."""

from __future__ import annotations

import json
from collections import Counter
from pipeline.context import Runtime
from scraper.cdx import read_cdx_jsonl
from scraper.snapshots import read_selected_snapshots
from utils.config import resolve_path


def run_report(runtime: Runtime) -> str:
    counts = runtime.store.counts()
    records = read_cdx_jsonl(resolve_path(runtime.config, "cdx_jsonl"))
    selected = read_selected_snapshots(resolve_path(runtime.config, "selected_snapshots"))
    parsed_dir = resolve_path(runtime.config, "parsed")
    html_dir = resolve_path(runtime.config, "raw_html")
    media_dir = resolve_path(runtime.config, "media")
    errors_path = resolve_path(runtime.config, "errors_jsonl")
    validation_path = resolve_path(runtime.config, "validation_csv")

    parsed = list(parsed_dir.glob("*.json"))
    parsed = [path for path in parsed if not path.name.endswith(".parsed.json")]
    html_files = list(html_dir.glob("*.html"))
    media_files = [path for path in media_dir.rglob("*") if path.is_file()]

    missing_fields: Counter[str] = Counter()
    for path in parsed:
        payload = json.loads(path.read_text(encoding="utf-8"))
        for field in payload.get("missing") or []:
            missing_fields[field] += 1

    failed: list[str] = []
    if errors_path.exists():
        for line in errors_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            failed.append(f"{item.get('url')} ({item.get('stage')})")

    scores: list[float] = []
    if validation_path.exists():
        for line in validation_path.read_text(encoding="utf-8").splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 8:
                try:
                    scores.append(float(parts[7]))
                except ValueError:
                    continue
    average = round(sum(scores) / len(scores), 1) if scores else 0.0

    report = f"""
Total URLs discovered:
{counts['urls'] or len({r.normalized_url for r in records})}

Snapshots found:
{counts['snapshots'] or len(records)}

Selected snapshots:
{len(selected)}

HTML downloaded:
{len(html_files)}

Pages parsed:
{len(parsed)}

Pages imported:
{counts['imports']}

Media found:
{len(media_files)}

Media imported:
{sum(1 for path in media_files)}

Failed:
{counts['errors']}

Average restoration score:
{average}%

Missing fields:
{dict(missing_fields) if missing_fields else 'none recorded'}

Failed URLs:
{chr(10).join(failed[:20]) if failed else 'none'}
""".strip()
    print(report)
    return report
