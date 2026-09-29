"""Load YAML config, field mapping, and environment overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"
FIELD_MAPPING_PATH = PROJECT_ROOT / "config" / "field_mapping.yaml"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    wayback_domain: str = "projecthindukush.com"
    wayback_path_prefix: str = "/incident/"
    wayback_delay: float = 2.0
    wayback_timeout: float = 60.0
    wayback_max_retries: int = 5
    wayback_user_agent: str = (
        "PKH-Restorer/0.1 (projecthindukush local restoration; "
        "respectful of archive.org rate limits)"
    )

    wp_url: str = "http://localhost/projecthindukush"
    wp_username: str = "admin"
    wp_password: str = "change-me"
    wp_admin_email: str = "admin@localhost.test"
    wp_site_title: str = "PROJECT HINDUKUSH"
    wp_path: str = r"C:\xampp1\htdocs\projecthindukush"
    php_binary: str = r"C:\xampp1\php\php.exe"
    mysql_binary: str = r"C:\xampp1\mysql\bin\mysql.exe"

    wp_db_name: str = "projecthindukush"
    wp_db_user: str = "root"
    wp_db_password: str = ""
    wp_db_host: str = "localhost"
    wp_db_root_password: str = ""

    restore_limit: int = 50
    ai_extraction_enabled: bool = False
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected mapping in {path}")
    return loaded


def load_config(root: Path | None = None) -> dict[str, Any]:
    load_dotenv(PROJECT_ROOT / ".env", override=False)
    base = root or PROJECT_ROOT
    config = load_yaml(base / "config" / "config.yaml")
    settings = Settings()

    config.setdefault("wayback", {})
    config["wayback"]["domain"] = settings.wayback_domain or config["wayback"].get("domain")
    config["wayback"]["path_prefix"] = settings.wayback_path_prefix or config["wayback"].get(
        "path_prefix"
    )
    config["wayback"]["delay_seconds"] = settings.wayback_delay
    config["wayback"]["timeout_seconds"] = settings.wayback_timeout
    config["wayback"]["max_retries"] = settings.wayback_max_retries
    config["wayback"]["user_agent"] = settings.wayback_user_agent
    config.setdefault("restore", {})
    config["restore"]["default_limit"] = settings.restore_limit
    config["settings"] = settings
    config["root"] = base
    return config


def load_field_mapping(root: Path | None = None) -> dict[str, Any]:
    base = root or PROJECT_ROOT
    return load_yaml(base / "config" / "field_mapping.yaml")


def resolve_path(config: dict[str, Any], key: str) -> Path:
    root: Path = config["root"]
    relative = config["paths"][key]
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
