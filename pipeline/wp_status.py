"""Verify the local XAMPP WordPress environment without logging secrets."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import httpx

from pipeline.context import Runtime
from pipeline.setup import _wp
from wordpress.client import WordPressClient, WordPressError

LOGGER = logging.getLogger("pkh.wpstatus")


def _http_ok(url: str) -> dict[str, Any]:
    try:
        response = httpx.get(url, timeout=20.0, follow_redirects=False)
        return {
            "ok": response.status_code < 500,
            "status": response.status_code,
            "url": url,
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "status": 0, "error": str(exc), "url": url}


def _mysql_running(runtime: Runtime) -> dict[str, Any]:
    settings = runtime.config["settings"]
    mysql = Path(settings.mysql_binary)
    if not mysql.exists():
        return {"ok": False, "error": f"mysql not found at {mysql}"}
    import subprocess

    result = subprocess.run(
        [str(mysql), "-u", settings.wp_db_user, "-h", settings.wp_db_host, "-e", "SELECT 1"],
        capture_output=True,
        text=True,
        check=False,
    )
    return {"ok": result.returncode == 0, "version": None}


def run_wp_status(runtime: Runtime) -> dict[str, Any]:
    settings = runtime.config["settings"]
    wp_root = Path(settings.wp_path)
    status: dict[str, Any] = {
        "runtime": "xampp",
        "wp_path": str(wp_root),
        "wordpress_exists": (wp_root / "wp-config.php").exists(),
        "apache": {},
        "mysql": {},
        "wordpress_installed": False,
        "frontend": {},
        "wp_admin": {},
        "rest": {},
        "auth": {"ok": False},
    }

    status["apache"] = _http_ok("http://localhost/")
    status["mysql"] = _mysql_running(runtime)
    status["frontend"] = _http_ok(settings.wp_url.rstrip("/") + "/")
    status["wp_admin"] = _http_ok(settings.wp_url.rstrip("/") + "/wp-login.php")

    if status["wordpress_exists"]:
        installed = _wp(runtime, ["core", "is-installed"], check=False)
        status["wordpress_installed"] = installed.returncode == 0
        core = _wp(runtime, ["core", "version"], check=False)
        status["wordpress_version"] = (core.stdout or "").strip() if core.returncode == 0 else None

    try:
        client = WordPressClient(settings.wp_url, settings.wp_username, settings.wp_password)
        rest = client.verify_rest()
        client.close()
        status["rest"] = {"ok": True, "name": rest.get("name"), "namespaces": rest.get("namespaces")}
        status["auth"] = {"ok": True, "note": "REST index reachable"}
    except WordPressError as exc:
        # Public REST index does not require auth; treat 200 /wp-json/ as REST ok.
        public = _http_ok(settings.wp_url.rstrip("/") + "/wp-json/")
        status["rest"] = {"ok": public.get("status") == 200, "public": public, "auth_error": str(exc)}
        status["auth"] = {"ok": False, "note": "HTTP auth not verified; live admin password is not in .env"}

    LOGGER.info(
        "wp-status apache=%s mysql=%s installed=%s frontend=%s rest=%s",
        status["apache"].get("status"),
        status["mysql"].get("ok"),
        status["wordpress_installed"],
        status["frontend"].get("status"),
        status["rest"].get("ok"),
    )
    print(json.dumps(status, indent=2))
    return status
