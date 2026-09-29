"""Import a ListingPro payload through local WP-CLI (XAMPP)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pipeline.wpcli import wp
from utils.config import PROJECT_ROOT


def json_from_output(text: str) -> Any:
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"WP-CLI output was not JSON: {text[-500:]}")


def sync_plugin(wp_path: str) -> Path:
    src = PROJECT_ROOT / "wp-plugin" / "pkh-restorer"
    dest = Path(wp_path) / "wp-content" / "plugins" / "pkh-restorer"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return dest


def set_listing_slug(php_binary: str, wp_path: str, slug: str = "incident") -> None:
    current = wp(php_binary, wp_path, ["option", "pluck", "listingpro_options", "listing_slug"], check=False)
    value = re.sub(r"PHP (Warning|Notice|Deprecated).*\n", "", current.stdout or "", flags=re.I).strip()
    lines = [line.strip() for line in value.splitlines() if line.strip() and "Warning" not in line]
    current_slug = lines[-1] if lines else ""
    if current_slug == slug:
        wp(php_binary, wp_path, ["rewrite", "flush", "--hard"], check=False)
        return
    snippet = PROJECT_ROOT / "data" / "tmp-set-listing-slug.php"
    snippet.write_text(
        "<?php\n"
        "$opts = get_option('listingpro_options');\n"
        "if (!is_array($opts)) { $opts = array(); }\n"
        f"$opts['listing_slug'] = '{slug}';\n"
        "update_option('listingpro_options', $opts);\n"
        "echo $opts['listing_slug'];\n",
        encoding="utf-8",
    )
    wp(php_binary, wp_path, ["eval-file", str(snippet)], check=False)
    wp(php_binary, wp_path, ["rewrite", "flush", "--hard"], check=False)


def backup_database(
    mysql_dump: str,
    db_name: str,
    db_user: str,
    db_host: str,
    destination: Path,
    db_password: str = "",
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    args = [mysql_dump, f"--user={db_user}", f"--host={db_host}", f"--result-file={destination}", db_name]
    if db_password:
        args.insert(1, f"--password={db_password}")
    import subprocess

    result = subprocess.run(args, check=False, text=True, capture_output=True)
    if result.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        raise RuntimeError(f"mysqldump failed: {result.stderr or result.stdout}")
    return destination


def import_payload_wpcli(
    php_binary: str,
    wp_path: str,
    payload: dict[str, Any],
    payload_path: Path,
) -> dict[str, Any]:
    sync_plugin(wp_path)
    wp(php_binary, wp_path, ["plugin", "activate", "pkh-restorer"], check=False)
    payload_path.parent.mkdir(parents=True, exist_ok=True)
    payload_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    cli_script = Path(wp_path) / "wp-content" / "plugins" / "pkh-restorer" / "includes" / "import-cli.php"
    env = os.environ.copy()
    env["PKH_IMPORT_JSON"] = str(payload_path)
    from pipeline.wpcli import ensure_wp_cli
    import subprocess

    phar = ensure_wp_cli(php_binary)
    command = [php_binary, str(phar), f"--path={wp_path}", "eval-file", str(cli_script)]
    result = subprocess.run(command, check=False, text=True, capture_output=True, env=env)
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode != 0:
        raise RuntimeError(f"WP-CLI import failed: {combined[-2000:]}")
    parsed = json_from_output(combined)
    if not isinstance(parsed, dict) or not parsed.get("id"):
        raise RuntimeError(f"WP-CLI import returned unexpected payload: {parsed}")
    return parsed
