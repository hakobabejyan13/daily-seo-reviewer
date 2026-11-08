"""Daily snapshots in storage.snapshot_dir (default data/snapshots/).

Layout: <snapshot_dir>/YYYY-MM-DD/{meta,robots,sitemap,...}.json
- One folder per UTC day. Re-running on the same day overwrites that day.
- Compact JSON (no indentation, sorted keys, UTF-8), written atomically
  (temp file + rename) so a crashed run never leaves half a file.
- meta.json holds the run time, the site from config and the status of every
  source, so readers can find "the last day source X was ok" without opening
  every file.

Retention deletes day folders older than storage.retention_days. It only
touches folders directly inside snapshot_dir whose name is a real
YYYY-MM-DD date; anything else there is left alone.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__

SCHEMA_VERSION = 1
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_SOURCE_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class SnapshotError(Exception):
    """A snapshot could not be written or read."""


def snapshot_root(cfg: dict[str, Any], base_dir: str | Path = ".") -> Path:
    """storage.snapshot_dir, resolved against base_dir when relative."""
    p = Path(cfg["storage"]["snapshot_dir"])
    return p if p.is_absolute() else Path(base_dir) / p


def utc_today(now: datetime | None = None) -> str:
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


def parse_day(name: str) -> date | None:
    """date for a 'YYYY-MM-DD' folder name, None for anything else."""
    if not _DAY_RE.match(name):
        return None
    try:
        return date.fromisoformat(name)
    except ValueError:
        return None


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def build_meta(cfg: dict[str, Any], day: str, records: dict[str, dict[str, Any]],
               run_started_at: datetime, run_finished_at: datetime | None = None) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "tool_version": __version__,
        "date": day,
        "run_started_at": run_started_at.astimezone(timezone.utc).isoformat(timespec="seconds"),
        "run_finished_at": (run_finished_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds"),
        "site": {k: cfg["site"][k] for k in ("name", "base_url", "sitemap_url")},
        "sources": {name: {"status": rec.get("status"), "error": rec.get("error")} for name, rec in records.items()},
    }


def write_snapshot(root: str | Path, day: str, records: dict[str, dict[str, Any]], meta: dict[str, Any]) -> Path:
    """Write meta.json plus one <source>.json per record into root/day/."""
    if parse_day(day) is None:
        raise SnapshotError(f"invalid snapshot date {day!r}, expected YYYY-MM-DD")
    for name in records:
        if not _SOURCE_RE.match(name) or name == "meta":
            raise SnapshotError(f"invalid source name {name!r}")
    folder = Path(root) / day
    folder.mkdir(parents=True, exist_ok=True)
    for name, rec in records.items():
        _write_atomic(folder / f"{name}.json", _dump(rec))
    _write_atomic(folder / "meta.json", _dump(meta))  # last: marks the day complete
    return folder


def list_snapshot_days(root: str | Path) -> list[str]:
    """Days (oldest first) that have a meta.json."""
    root = Path(root)
    if not root.is_dir():
        return []
    days = [p.name for p in root.iterdir()
            if p.is_dir() and not p.is_symlink() and parse_day(p.name) and (p / "meta.json").is_file()]
    return sorted(days)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise SnapshotError(f"cannot read {path}: {e}") from None


def read_meta(root: str | Path, day: str) -> dict[str, Any]:
    return _read_json(Path(root) / day / "meta.json")


def read_source(root: str | Path, day: str, source: str) -> dict[str, Any] | None:
    """One source record for a day, or None if that file does not exist."""
    if not _SOURCE_RE.match(source):
        raise SnapshotError(f"invalid source name {source!r}")
    path = Path(root) / day / f"{source}.json"
    return _read_json(path) if path.is_file() else None


def read_snapshot(root: str | Path, day: str) -> dict[str, Any]:
    """All files of one day: {'meta': {...}, '<source>': {...}, ...}."""
    folder = Path(root) / day
    if not (folder / "meta.json").is_file():
        raise SnapshotError(f"no snapshot for {day} in {root}")
    return {p.stem: _read_json(p) for p in sorted(folder.glob("*.json"))}


def latest_previous_day(root: str | Path, before: str) -> str | None:
    """The newest snapshot day strictly before `before`."""
    earlier = [d for d in list_snapshot_days(root) if d < before]
    return earlier[-1] if earlier else None


def last_day_with_status(root: str | Path, source: str, before: str,
                         statuses: tuple[str, ...] = ("ok",)) -> tuple[str, dict[str, Any]] | None:
    """Newest day before `before` where `source` had one of `statuses`
    (default: ok). Returns (day, record) or None. Unreadable days are skipped."""
    for day in reversed([d for d in list_snapshot_days(root) if d < before]):
        try:
            status = read_meta(root, day).get("sources", {}).get(source, {}).get("status")
            if status in statuses:
                rec = read_source(root, day, source)
                if rec is not None:
                    return day, rec
        except SnapshotError:
            continue
    return None


def apply_retention(root: str | Path, today: str, retention_days: int) -> list[str]:
    """Delete day folders older than retention_days before `today`.
    Returns the deleted day names (oldest first)."""
    today_d = parse_day(today)
    if today_d is None:
        raise SnapshotError(f"invalid date {today!r}")
    if retention_days < 1:
        raise SnapshotError("retention_days must be >= 1")
    root = Path(root)
    if not root.is_dir():
        return []
    root_resolved = root.resolve()
    cutoff = today_d - timedelta(days=retention_days)
    deleted: list[str] = []
    for p in sorted(root.iterdir()):
        d = parse_day(p.name)
        if d is None or d >= cutoff or p.is_symlink() or not p.is_dir():
            continue
        if p.resolve().parent != root_resolved:  # paranoia: only direct children
            continue
        shutil.rmtree(p)
        deleted.append(p.name)
    return deleted
