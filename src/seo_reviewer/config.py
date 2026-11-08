"""Load and validate config.yaml.

Validation collects every problem first and then raises one ConfigError that
lists them all, so a broken config is fixed in one pass.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml


class ConfigError(Exception):
    """config.yaml is missing, unreadable, or does not match the schema."""


# Type markers used in SCHEMA.
STR = "non-empty string"
URL = "http(s) URL"
INT_POS = "integer > 0"
INT_NONNEG = "integer >= 0"
NUM_POS = "number > 0"
NUM_NONNEG = "number >= 0"
BOOL = "true/false"
LIST = "list"
LIST_STR = "list of strings"

# Every key the program reads. Nested dicts are sections.
SCHEMA: dict[str, Any] = {
    "site": {
        "name": STR,
        "base_url": URL,
        "sitemap_url": URL,
    },
    "crawler": {
        "user_agent": STR,
        "requests_per_second": NUM_POS,
        "timeout_seconds": NUM_POS,
        "max_retries": INT_NONNEG,
        "respect_robots_txt": BOOL,
        "max_pages": INT_POS,
        "extra_urls": LIST_STR,
    },
    "sitemap": {
        "max_child_sitemaps": INT_POS,
        "max_urls": INT_POS,
    },
    "links": {
        "enabled": BOOL,
        "max_links": INT_POS,
        "check_external": BOOL,
        "method": STR,
    },
    "psi": {
        "enabled": BOOL,
        "api_key_env": STR,
        "urls": LIST_STR,
        "strategies": LIST_STR,
        "timeout_seconds": NUM_POS,
        "max_retries": INT_NONNEG,
        "regression_confirmations": INT_POS,
    },
    "thresholds": {
        "title_length": {"min": INT_NONNEG, "max": INT_POS},
        "meta_description_length": {"min": INT_NONNEG, "max": INT_POS},
        "psi_performance_drop_points": NUM_NONNEG,
        "lab_tolerance": {"lcp_ms": NUM_NONNEG, "cls": NUM_NONNEG, "tbt_ms": NUM_NONNEG},
    },
    "llm": {
        "enabled": BOOL,
        "provider": STR,
        "model": STR,
        "api_key_env": STR,
        "endpoint": URL,
        "max_output_tokens": INT_POS,
        "thinking_budget": INT_NONNEG,
        "timeout_seconds": NUM_POS,
    },
    "delivery": {
        "telegram": {
            "enabled": BOOL,
            "bot_token_env": STR,
            "chat_id_env": STR,
            "parse_mode": STR,
            "max_findings_in_message": INT_POS,
        },
        "report_dir": STR,
        "report_base_url": URL,
    },
    "storage": {
        "snapshot_dir": STR,
        "retention_days": INT_POS,
    },
    "runtime": {
        "budget_seconds": INT_POS,
    },
}


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _check_value(kind: str, v: Any) -> bool:
    if kind == STR:
        return isinstance(v, str) and v.strip() != ""
    if kind == URL:
        if not isinstance(v, str):
            return False
        p = urlparse(v)
        return p.scheme in ("http", "https") and bool(p.netloc)
    if kind == INT_POS:
        return isinstance(v, int) and not isinstance(v, bool) and v > 0
    if kind == INT_NONNEG:
        return isinstance(v, int) and not isinstance(v, bool) and v >= 0
    if kind == NUM_POS:
        return _is_number(v) and v > 0
    if kind == NUM_NONNEG:
        return _is_number(v) and v >= 0
    if kind == BOOL:
        return isinstance(v, bool)
    if kind == LIST:
        return isinstance(v, list)
    if kind == LIST_STR:
        return isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
    raise AssertionError(f"unknown schema type {kind!r}")


def _walk(schema: dict[str, Any], data: Any, prefix: str, errors: list[str]) -> None:
    if not isinstance(data, dict):
        errors.append(f"{prefix or '<root>'}: must be a mapping (section), got {type(data).__name__}")
        return
    for key, kind in schema.items():
        path = f"{prefix}.{key}" if prefix else key
        if key not in data:
            errors.append(f"{path}: missing")
            continue
        value = data[key]
        if isinstance(kind, dict):
            _walk(kind, value, path, errors)
        elif not _check_value(kind, value):
            errors.append(f"{path}: must be a {kind}, got {value!r}")


def _cross_checks(cfg: dict[str, Any], errors: list[str]) -> None:
    site = cfg.get("site", {})
    base, sitemap = site.get("base_url"), site.get("sitemap_url")
    if isinstance(base, str) and isinstance(sitemap, str):
        if urlparse(base).netloc and urlparse(sitemap).netloc and urlparse(base).netloc != urlparse(sitemap).netloc:
            errors.append("site.sitemap_url: must be on the same host as site.base_url")
    th = cfg.get("thresholds", {})
    for name in ("title_length", "meta_description_length"):
        rng = th.get(name)
        if isinstance(rng, dict) and _is_number(rng.get("min")) and _is_number(rng.get("max")):
            if rng["min"] > rng["max"]:
                errors.append(f"thresholds.{name}: min must be <= max")
    strategies = cfg.get("psi", {}).get("strategies")
    if isinstance(strategies, list):
        bad = [s for s in strategies if s not in ("mobile", "desktop")]
        if bad:
            errors.append(f"psi.strategies: only 'mobile' and 'desktop' are allowed, got {bad!r}")
    method = cfg.get("links", {}).get("method")
    if isinstance(method, str) and method.upper() not in ("HEAD", "GET"):
        errors.append(f"links.method: must be HEAD or GET, got {method!r}")


def validate_config(cfg: Any) -> dict[str, Any]:
    """Return cfg unchanged if valid, otherwise raise ConfigError listing all problems."""
    errors: list[str] = []
    _walk(SCHEMA, cfg, "", errors)
    if isinstance(cfg, dict):
        _cross_checks(cfg, errors)
    if errors:
        raise ConfigError("Invalid config:\n  - " + "\n  - ".join(errors))
    # Normalise: no trailing slash on base_url, so URL joins are predictable.
    cfg["site"]["base_url"] = cfg["site"]["base_url"].rstrip("/")
    return cfg


def load_config(path: str | Path = "config.yaml") -> dict[str, Any]:
    """Read, parse and validate a config file."""
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {p}") from None
    except OSError as e:
        raise ConfigError(f"Cannot read config file {p}: {e}") from None
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"Config file {p} is not valid YAML: {e}") from None
    if data is None:
        raise ConfigError(f"Config file {p} is empty")
    return validate_config(data)
