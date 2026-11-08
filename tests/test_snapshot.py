import json
from datetime import datetime, timezone

import pytest

from seo_reviewer import snapshot as snap


def rec(source, status, **extra):
    return {"source": source, "status": status, "error": None if status == "ok" else "boom", **extra}


def write_day(cfg, root, day, robots="ok", sitemap="ok"):
    records = {"robots": rec("robots", robots), "sitemap": rec("sitemap", sitemap, day=day)}
    meta = snap.build_meta(cfg, day, records, datetime(2026, 11, 8, 5, 0, tzinfo=timezone.utc))
    return snap.write_snapshot(root, day, records, meta)


def test_write_and_read_roundtrip(cfg, tmp_path):
    folder = write_day(cfg, tmp_path, "2026-11-08")
    assert sorted(p.name for p in folder.iterdir()) == ["meta.json", "robots.json", "sitemap.json"]
    text = (folder / "sitemap.json").read_text(encoding="utf-8")
    assert "\n" not in text.rstrip("\n") and ": " not in text  # compact
    data = snap.read_snapshot(tmp_path, "2026-11-08")
    assert data["meta"]["date"] == "2026-11-08"
    assert data["meta"]["site"]["base_url"] == "https://example.test"
    assert data["meta"]["sources"] == {"robots": {"status": "ok", "error": None},
                                       "sitemap": {"status": "ok", "error": None}}
    assert data["meta"]["run_started_at"] == "2026-11-08T05:00:00+00:00"
    assert data["sitemap"]["day"] == "2026-11-08"
    assert not list(folder.glob("*.tmp"))


def test_rewrite_same_day_overwrites(cfg, tmp_path):
    write_day(cfg, tmp_path, "2026-11-08", sitemap="failed")
    write_day(cfg, tmp_path, "2026-11-08", sitemap="ok")
    assert snap.read_meta(tmp_path, "2026-11-08")["sources"]["sitemap"]["status"] == "ok"


def test_invalid_names_rejected(cfg, tmp_path):
    with pytest.raises(snap.SnapshotError):
        snap.write_snapshot(tmp_path, "2026-11-8", {}, {})
    with pytest.raises(snap.SnapshotError):
        snap.write_snapshot(tmp_path, "2026-11-08", {"../evil": {}}, {})
    with pytest.raises(snap.SnapshotError):
        snap.write_snapshot(tmp_path, "2026-11-08", {"meta": {}}, {})


def test_list_ignores_junk_and_incomplete_days(cfg, tmp_path):
    write_day(cfg, tmp_path, "2026-11-06")
    write_day(cfg, tmp_path, "2026-11-08")
    (tmp_path / "2026-11-07").mkdir()          # no meta.json: incomplete
    (tmp_path / "notes").mkdir()
    (tmp_path / "2026-02-30").mkdir()          # not a real date
    (tmp_path / "README.md").write_text("x")
    assert snap.list_snapshot_days(tmp_path) == ["2026-11-06", "2026-11-08"]
    assert snap.list_snapshot_days(tmp_path / "missing") == []


def test_latest_previous_day(cfg, tmp_path):
    for d in ("2026-11-05", "2026-11-06", "2026-11-08"):
        write_day(cfg, tmp_path, d)
    assert snap.latest_previous_day(tmp_path, "2026-11-08") == "2026-11-06"
    assert snap.latest_previous_day(tmp_path, "2026-11-09") == "2026-11-08"
    assert snap.latest_previous_day(tmp_path, "2026-11-05") is None


def test_last_day_with_status_skips_failed_days(cfg, tmp_path):
    write_day(cfg, tmp_path, "2026-11-05", sitemap="ok")
    write_day(cfg, tmp_path, "2026-11-06", sitemap="failed")
    write_day(cfg, tmp_path, "2026-11-07", sitemap="partial")
    day, record = snap.last_day_with_status(tmp_path, "sitemap", "2026-11-08")
    assert day == "2026-11-05" and record["day"] == "2026-11-05"
    day, _ = snap.last_day_with_status(tmp_path, "sitemap", "2026-11-08", statuses=("ok", "partial"))
    assert day == "2026-11-07"
    assert snap.last_day_with_status(tmp_path, "robots", "2026-11-08")[0] == "2026-11-07"
    assert snap.last_day_with_status(tmp_path, "psi", "2026-11-08") is None
    assert snap.last_day_with_status(tmp_path, "sitemap", "2026-11-05") is None


def test_last_day_with_status_skips_corrupt_meta(cfg, tmp_path):
    write_day(cfg, tmp_path, "2026-11-05")
    write_day(cfg, tmp_path, "2026-11-06")
    (tmp_path / "2026-11-06" / "meta.json").write_text("{not json", encoding="utf-8")
    assert snap.last_day_with_status(tmp_path, "sitemap", "2026-11-08")[0] == "2026-11-05"


def test_retention_deletes_only_old_day_folders(cfg, tmp_path):
    for d in ("2026-08-09", "2026-08-10", "2026-08-11", "2026-11-08"):
        write_day(cfg, tmp_path, d)
    (tmp_path / "2020-01-01").mkdir()                   # old, incomplete: still a day folder
    (tmp_path / "keep-me").mkdir()                      # not a date: untouched
    (tmp_path / "2020-01-02.json").write_text("{}")     # a file, not a folder: untouched
    deleted = snap.apply_retention(tmp_path, "2026-11-08", 90)   # cutoff 2026-08-10
    assert deleted == ["2020-01-01", "2026-08-09"]
    remaining = sorted(p.name for p in tmp_path.iterdir())
    assert remaining == ["2020-01-02.json", "2026-08-10", "2026-08-11", "2026-11-08", "keep-me"]


def test_retention_does_not_follow_symlinks(cfg, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("x")
    root = tmp_path / "snaps"
    root.mkdir()
    try:
        (root / "2000-01-01").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks not available")
    assert snap.apply_retention(root, "2026-11-08", 90) == []
    assert (outside / "precious.txt").exists()


def test_retention_bad_args(tmp_path):
    with pytest.raises(snap.SnapshotError):
        snap.apply_retention(tmp_path, "nope", 90)
    with pytest.raises(snap.SnapshotError):
        snap.apply_retention(tmp_path, "2026-11-08", 0)
    assert snap.apply_retention(tmp_path / "missing", "2026-11-08", 90) == []


def test_snapshot_root_and_utc_today(cfg, tmp_path):
    assert snap.snapshot_root(cfg, tmp_path) == tmp_path / "data" / "snapshots"
    late = datetime(2026, 11, 8, 23, 30, tzinfo=timezone.utc)
    assert snap.utc_today(late) == "2026-11-08"
    assert json.loads(json.dumps(snap.utc_today()))  # plain string
