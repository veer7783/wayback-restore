"""Offline inspection of supplied ListingPro packages. Does not install anything."""

from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
THEME = ROOT / "theme"
PLUGIN_ZIP = THEME / "listingpro" / "include" / "plugins" / "listingpro-plugin.zip"

PT_RE = re.compile(r"register_post_type\(\s*['\"]([^'\"]+)['\"]")
TX_RE = re.compile(r"register_taxonomy\(\s*['\"]([^'\"]+)['\"]")
META_RE = re.compile(
    r"['\"](lp_[a-zA-Z0-9_]+|gallery_image_ids|gAddress|latitude|longitude|business_logo|tagline_text)['\"]"
)


def inspect_zip(path: Path) -> dict:
    post_types: set[str] = set()
    taxonomies: set[str] = set()
    meta: set[str] = set()
    files: list[str] = []
    if not path.exists():
        return {"exists": False, "path": str(path)}
    with zipfile.ZipFile(path) as archive:
        files = archive.namelist()
        for name in files:
            if not name.lower().endswith(".php"):
                continue
            text = archive.read(name).decode("utf-8", errors="ignore")
            post_types.update(PT_RE.findall(text))
            taxonomies.update(TX_RE.findall(text))
            meta.update(META_RE.findall(text))
    return {
        "exists": True,
        "path": str(path),
        "file_count": len(files),
        "post_types": sorted(post_types),
        "taxonomies": sorted(taxonomies),
        "meta_keys": sorted(meta),
    }


def inspect_demo_xml(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    text = path.read_text(encoding="utf-8", errors="ignore")
    post_types = sorted(set(re.findall(r"<wp:post_type><!\[CDATA\[([^\]]+)\]\]>", text)))
    taxonomies = sorted(set(re.findall(r"<wp:domain><!\[CDATA\[([^\]]+)\]\]>", text)))
    meta_keys = sorted(set(re.findall(r"<wp:meta_key><!\[CDATA\[([^\]]+)\]\]>", text)))
    return {
        "exists": True,
        "path": str(path),
        "bytes": path.stat().st_size,
        "post_types": post_types,
        "taxonomies": taxonomies,
        "meta_keys": meta_keys,
        "imported": False,
    }


def main() -> None:
    payload = {
        "listingpro_theme_style": (THEME / "listingpro" / "style.css").read_text(encoding="utf-8", errors="ignore")[:400],
        "listingpro_plugin": inspect_zip(PLUGIN_ZIP),
        "listingpro_reviews": inspect_zip(THEME / "listingpro" / "include" / "plugins" / "listingpro-reviews.zip"),
        "listingpro_ads": inspect_zip(THEME / "listingpro" / "include" / "plugins" / "listingpro-ads.zip"),
        "redux": inspect_zip(THEME / "listingpro" / "include" / "plugins" / "redux-framework.zip"),
        "cubewp_framework": inspect_zip(THEME / "listingpro" / "include" / "plugins" / "cubewp-framework.zip"),
        "js_composer": inspect_zip(THEME / "listingpro" / "include" / "plugins" / "js_composer.zip"),
        "child_theme": inspect_zip(THEME / "Child Theme" / "listingpro-child.zip"),
        "bulk_import_addon": inspect_zip(THEME / "Bulk Import Add-on" / "listingpro-Bulk-import-addon.zip"),
        "demo_classic": inspect_demo_xml(THEME / "Live-demo-Content" / "classic" / "demo-content.xml"),
        "demo_placespro": inspect_demo_xml(THEME / "Live-demo-Content" / "placespro" / "demo-content.xml"),
    }
    out = ROOT / "data" / "exports" / "package-inspection.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({k: {kk: vv for kk, vv in (v.items() if isinstance(v, dict) else [])} for k, v in payload.items() if isinstance(v, dict)}, indent=2)[:4000])
    print("Wrote", out)


if __name__ == "__main__":
    main()
