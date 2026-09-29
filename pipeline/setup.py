"""Local WordPress Docker setup and ListingPro inspection/install."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from pipeline.context import Runtime
from pipeline.wpcli import wp as wpcli_run
from utils.config import resolve_path
from wordpress.client import WordPressClient, WordPressError
from wordpress.listingpro import inspect_php_tree, inspect_theme_zip, merge_schema, write_schema

LOGGER = logging.getLogger("pkh.setup")


def _wp(runtime: Runtime, args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    settings = runtime.config["settings"]
    return wpcli_run(settings.php_binary, settings.wp_path, args, check=check)


def _wp_ok(result: subprocess.CompletedProcess[str]) -> str:
    text = (result.stdout or "") + (result.stderr or "")
    return text.strip()


def run_setup(runtime: Runtime) -> None:
    """Verify the XAMPP WordPress site and install the restoration plugin."""
    settings = runtime.config["settings"]
    wp_root = Path(settings.wp_path)
    if not (wp_root / "wp-config.php").exists():
        raise FileNotFoundError(f"WordPress not found at {wp_root}")

    installed = _wp(runtime, ["core", "is-installed"], check=False)
    if installed.returncode != 0:
        raise RuntimeError(f"WordPress is not installed at {wp_root}: {_wp_ok(installed)}")

    _wp(runtime, ["option", "update", "siteurl", settings.wp_url.rstrip("/")], check=False)
    _wp(runtime, ["option", "update", "home", settings.wp_url.rstrip("/")], check=False)
    _wp(runtime, ["rewrite", "structure", "/%postname%/", "--hard"], check=False)

    plugin_src = runtime.root / "wp-plugin" / "pkh-restorer"
    plugin_dst = wp_root / "wp-content" / "plugins" / "pkh-restorer"
    if plugin_dst.exists():
        shutil.rmtree(plugin_dst)
    shutil.copytree(plugin_src, plugin_dst)
    _wp(runtime, ["plugin", "activate", "pkh-restorer"], check=False)
    LOGGER.info("XAMPP WordPress setup complete at %s", settings.wp_url)


BUNDLED_REQUIRED_PLUGINS = (
    "redux-framework.zip",
    "listingpro-plugin.zip",
    "listingpro-reviews.zip",
    "listingpro-ads.zip",
    "cubewp-framework.zip",
)


def install_listingpro_stack(runtime: Runtime) -> dict[str, Any]:
    """Install ListingPro 2.9.12 and bundled required plugins only."""
    settings = runtime.config["settings"]
    wp_root = Path(settings.wp_path)
    theme_dir = runtime.root / "theme" / "listingpro"
    style = theme_dir / "style.css"
    if not style.exists():
        raise FileNotFoundError("theme/listingpro/style.css not found. Expected extracted ListingPro 2.9.12.")
    style_text = style.read_text(encoding="utf-8", errors="ignore")
    if "Version: 2.9.12" not in style_text:
        raise RuntimeError("Refusing to install: theme/listingpro is not Version 2.9.12")

    dest_theme = wp_root / "wp-content" / "themes" / "listingpro"
    if dest_theme.exists():
        shutil.rmtree(dest_theme)
    shutil.copytree(theme_dir, dest_theme, ignore=shutil.ignore_patterns("*.zip"))
    # Keep bundled plugin zips available inside the theme copy for ListingPro's TGMPA.
    bundled_src = theme_dir / "include" / "plugins"
    bundled_dst = dest_theme / "include" / "plugins"
    bundled_dst.mkdir(parents=True, exist_ok=True)
    for filename in (*BUNDLED_REQUIRED_PLUGINS, "js_composer.zip"):
        src = bundled_src / filename
        if src.exists():
            shutil.copy2(src, bundled_dst / filename)

    activated = _wp(runtime, ["theme", "activate", "listingpro"], check=False)
    if activated.returncode != 0:
        raise RuntimeError(f"Failed to activate ListingPro: {_wp_ok(activated)}")

    installed_plugins: list[str] = []
    failed_plugins: list[str] = []
    for filename in BUNDLED_REQUIRED_PLUGINS:
        local = bundled_src / filename
        if not local.exists():
            failed_plugins.append(f"{filename} (missing from package)")
            continue
        result = _wp(runtime, ["plugin", "install", str(local), "--activate", "--force"], check=False)
        if result.returncode == 0:
            installed_plugins.append(filename)
        else:
            failed_plugins.append(f"{filename}: {_wp_ok(result)}")

    child_zip = runtime.root / "theme" / "Child Theme" / "listingpro-child.zip"
    child_status = "not-found"
    if child_zip.exists():
        installed = _wp(runtime, ["theme", "install", str(child_zip), "--force"], check=False)
        child_status = "installed-inactive" if installed.returncode == 0 else f"install-failed: {_wp_ok(installed)}"

    plugin_src = runtime.root / "wp-plugin" / "pkh-restorer"
    plugin_dst = wp_root / "wp-content" / "plugins" / "pkh-restorer"
    if plugin_dst.exists():
        shutil.rmtree(plugin_dst)
    shutil.copytree(plugin_src, plugin_dst)
    _wp(runtime, ["plugin", "activate", "pkh-restorer"], check=False)

    report = {
        "theme": "listingpro",
        "version": "2.9.12",
        "activated": activated.returncode == 0,
        "bundled_plugins_installed": installed_plugins,
        "bundled_plugins_failed": failed_plugins,
        "child_theme": child_status,
        "demo_imported": False,
        "runtime": "xampp",
        "wp_path": str(wp_root),
    }
    LOGGER.info("ListingPro stack installed: theme=2.9.12 plugins=%s child=%s", installed_plugins, child_status)
    return report


def install_theme(runtime: Runtime, zip_path: Path) -> None:
    if not zip_path.exists():
        raise FileNotFoundError(
            f"Theme ZIP not found: {zip_path}. Place your legitimate ListingPro ZIP at theme/listingpro.zip"
        )
    result = _wp(runtime, ["theme", "install", str(zip_path.resolve()), "--force", "--activate"], check=False)
    if result.returncode != 0:
        raise RuntimeError(_wp_ok(result))
    LOGGER.info("Installed theme ZIP %s", zip_path.name)


def _wp_json(runtime: Runtime, args: list[str]) -> Any:
    result = _wp(runtime, args, check=False)
    text = (result.stdout or "").strip()
    if result.returncode != 0 or not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def inspect_listingpro(runtime: Runtime) -> dict[str, Any]:
    schema_parts = []
    for zip_path in (
        runtime.root / "theme" / "listingpro-2.9.12.zip",
        runtime.root / "theme" / "listingpro.zip",
        runtime.root / "theme" / "listingpro" / "include" / "plugins" / "listingpro-plugin.zip",
    ):
        if zip_path.exists():
            schema_parts.append(inspect_theme_zip(zip_path))

    extracted = runtime.root / "theme" / "listingpro"
    if extracted.exists():
        schema_parts.append(inspect_php_tree(extracted))

    live: dict[str, Any] = {
        "verified": False,
        "post_types": [],
        "taxonomies": [],
        "meta_fields": [],
        "theme": None,
        "plugins": None,
        "core_version": None,
        "php_version": None,
    }
    try:
        types = _wp_json(runtime, ["post-type", "list", "--fields=name,label,public", "--format=json"])
        taxes = _wp_json(runtime, ["taxonomy", "list", "--fields=name,label,object_type", "--format=json"])
        theme = _wp_json(runtime, ["theme", "list", "--format=json"])
        plugins = _wp_json(runtime, ["plugin", "list", "--format=json"])
        core = _wp(runtime, ["core", "version"], check=False)
        php = _wp(runtime, ["eval", "echo PHP_VERSION;"], check=False)
        if isinstance(types, list):
            live["post_types"] = types
        if isinstance(taxes, list):
            live["taxonomies"] = taxes
        live["theme"] = theme
        live["plugins"] = plugins
        live["core_version"] = (core.stdout or "").strip() if core.returncode == 0 else None
        live["php_version"] = (php.stdout or "").strip() if php.returncode == 0 else None
        live["verified"] = bool(live["post_types"])
    except FileNotFoundError:
        LOGGER.warning("WP-CLI is not available; writing file-inspection schema only")
    except Exception:
        LOGGER.exception("WP-CLI schema inspect failed; using file inspection only")

    file_schema = merge_schema(*schema_parts)
    schema = {
        "verified_live": live["verified"],
        "file_inspection": file_schema,
        "live": live,
        "post_types": live["post_types"] or file_schema.get("post_types") or [],
        "taxonomies": live["taxonomies"] or file_schema.get("taxonomies") or [],
        "meta_fields": file_schema.get("meta_fields") or [],
        "important_fields": [
            {
                "key": "lp_listingpro_options",
                "type": "serialized array / post meta",
                "where": "listingpro-plugin.zip + demo XML (not imported)",
                "purpose": "Primary ListingPro listing options bag",
                "example": None,
            },
            {
                "key": "gAddress",
                "type": "string inside lp_listingpro_options",
                "where": "listingpro-plugin + Bulk Import Add-on + theme map templates",
                "purpose": "Google/map address",
                "example": None,
            },
            {
                "key": "latitude",
                "type": "string/float inside lp_listingpro_options",
                "where": "listingpro-plugin + Bulk Import Add-on + theme data-lat",
                "purpose": "Map latitude",
                "example": None,
            },
            {
                "key": "longitude",
                "type": "string/float inside lp_listingpro_options",
                "where": "listingpro-plugin + Bulk Import Add-on + theme data-lan",
                "purpose": "Map longitude",
                "example": None,
            },
            {
                "key": "gallery_image_ids",
                "type": "comma-separated attachment IDs",
                "where": "Bulk Import Add-on listingpro_addon_gallery_images + theme gallery.php + demo XML",
                "purpose": "Listing gallery",
                "example": None,
            },
            {
                "key": "_thumbnail_id",
                "type": "WordPress featured image",
                "where": "demo XML (not imported) + core WordPress",
                "purpose": "Featured image",
                "example": None,
            },
            {
                "key": "business_logo",
                "type": "URL inside lp_listingpro_options",
                "where": "Bulk Import Add-on logo import",
                "purpose": "Listing logo",
                "example": None,
            },
            {
                "key": "lp_listingpro_options_fields",
                "type": "post meta",
                "where": "demo XML (not imported)",
                "purpose": "Additional/custom form field values",
                "example": None,
            },
        ],
        "source": {
            "listingpro_2_9_12_zip_present": (runtime.root / "theme" / "listingpro-2.9.12.zip").exists(),
            "extracted_theme_present": extracted.exists(),
            "inspected_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
    }
    write_schema(schema, resolve_path(runtime.config, "listingpro_schema"))
    md_path = runtime.root / "data" / "exports" / "listingpro-schema.md"
    md_path.write_text(_schema_markdown(schema), encoding="utf-8")
    LOGGER.info("Wrote ListingPro schema live_verified=%s", live["verified"])
    return schema


def _schema_markdown(schema: dict[str, Any]) -> str:
    live = schema.get("live") or {}
    lines = [
        "# ListingPro schema",
        "",
        f"Live WordPress inspection: **{'verified' if schema.get('verified_live') else 'not verified'}**",
        "",
        "## Post types",
        "",
        json.dumps(schema.get("post_types"), indent=2),
        "",
        "## Taxonomies",
        "",
        json.dumps(schema.get("taxonomies"), indent=2),
        "",
        "## Important fields",
        "",
    ]
    for field in schema.get("important_fields") or []:
        lines.extend(
            [
                f"### `{field['key']}`",
                "",
                f"- Type: {field['type']}",
                f"- Where discovered: {field['where']}",
                f"- Purpose: {field['purpose']}",
                f"- Example: {field['example'] if field['example'] is not None else 'none yet (clean database)'}",
                "",
            ]
        )
    lines.extend(
        [
            "## Live theme/plugins",
            "",
            f"- WordPress: {live.get('core_version')}",
            f"- PHP: {live.get('php_version')}",
            "",
            "```json",
            json.dumps({"theme": live.get("theme"), "plugins": live.get("plugins")}, indent=2),
            "```",
            "",
        ]
    )
    return "\n".join(lines)
