"""Local WP-CLI runner for the XAMPP WordPress site. Does not use Docker."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

LOGGER = logging.getLogger("pkh.wpcli")

WP_CLI_PHAR = Path(__file__).resolve().parents[1] / "tools" / "wp-cli.phar"


def ensure_wp_cli(php_binary: str) -> Path:
    if WP_CLI_PHAR.exists() and WP_CLI_PHAR.stat().st_size > 1000:
        return WP_CLI_PHAR
    WP_CLI_PHAR.parent.mkdir(parents=True, exist_ok=True)
    import urllib.request

    url = "https://raw.githubusercontent.com/wp-cli/builds/gh-pages/phar/wp-cli.phar"
    LOGGER.info("Downloading WP-CLI")
    urllib.request.urlretrieve(url, WP_CLI_PHAR)
    return WP_CLI_PHAR


def wp(
    php_binary: str,
    wp_path: str,
    args: list[str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    phar = ensure_wp_cli(php_binary)
    command = [php_binary, str(phar), f"--path={wp_path}", *args]
    LOGGER.info("WP-CLI %s", " ".join(args))
    return subprocess.run(command, check=check, text=True, capture_output=True)
