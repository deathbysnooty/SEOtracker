#!/usr/bin/env python3
"""Export rankings.db data to docs/data.json for the GitHub Pages dashboard."""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone, date

# SGT is UTC+8
SGT = timezone(timedelta(hours=8))
SCHEDULE_HOURS_SGT = [7, 13, 19]  # 7AM, 1PM, 7PM SGT

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rankings.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs")
OUTPUT_PATH = os.path.join(OUTPUT_DIR, "data.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def dict_factory(cursor, row):
    """Convert sqlite3 rows to dicts."""
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def get_db():
    if not os.path.exists(DB_PATH):
        return None
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = dict_factory
    return conn


def compute_next_run(last_run_str):
    """Given the last run ISO timestamp, find the next scheduled SGT slot."""
    if not last_run_str:
        return None
    try:
        last_run = datetime.fromisoformat(last_run_str)
    except (ValueError, TypeError):
        return None

    # If naive, assume UTC
    if last_run.tzinfo is None:
        last_run = last_run.replace(tzinfo=timezone.utc)

    last_run_sgt = last_run.astimezone(SGT)

    # Try each scheduled hour today (SGT), then tomorrow
    for day_offset in range(0, 3):
        candidate_date = last_run_sgt.date() + timedelta(days=day_offset)
        for hour in SCHEDULE_HOURS_SGT:
            candidate = datetime(
                candidate_date.year, candidate_date.month, candidate_date.day,
                hour, 0, 0, tzinfo=SGT
            )
            if candidate > last_run_sgt:
                return candidate.isoformat()

    return None


def build_meta(conn, config):
    meta = {
        "last_run": None,
        "next_run": None,
        "run_count_today": 0,
        "api_used_this_month": 0,
        "api_limit": config.get("serpapi_monthly_limit", 0),
        "api_percent": 0,
    }
    if conn is None:
        return meta

    cur = conn.cursor()

    # last_run
    cur.execute("SELECT MAX(run_time) AS last_run FROM run_log")
    row = cur.fetchone()
    last_run = row["last_run"] if row else None
    meta["last_run"] = last_run
    meta["next_run"] = compute_next_run(last_run)

    # run_count_today (SGT date)
    today_sgt = datetime.now(SGT).strftime("%Y-%m-%d")
    cur.execute(
        "SELECT COUNT(DISTINCT run_time) AS cnt FROM run_log "
        "WHERE date(run_time) = ?",
        (today_sgt,),
    )
    row = cur.fetchone()
    meta["run_count_today"] = row["cnt"] if row else 0

    # api_used_this_month
    month_start = datetime.now(SGT).strftime("%Y-%m-01")
    cur.execute(
        "SELECT COALESCE(SUM(api_calls_used), 0) AS total FROM run_log "
        "WHERE run_time >= ?",
        (month_start,),
    )
    row = cur.fetchone()
    meta["api_used_this_month"] = row["total"] if row else 0

    # api_percent
    limit = meta["api_limit"]
    if limit > 0:
        meta["api_percent"] = round(meta["api_used_this_month"] / limit * 100)
    else:
        meta["api_percent"] = 0

    return meta


def build_rankings(conn):
    result = {"latest": [], "previous": [], "history_14_runs": []}
    if conn is None:
        return result

    cur = conn.cursor()

    # Get distinct run_times ordered descending
    cur.execute(
        "SELECT DISTINCT run_time FROM rankings ORDER BY run_time DESC"
    )
    run_times = [r["run_time"] for r in cur.fetchall()]

    if not run_times:
        return result

    # latest
    latest_rt = run_times[0]
    cur.execute("SELECT * FROM rankings WHERE run_time = ?", (latest_rt,))
    result["latest"] = cur.fetchall()

    # previous
    if len(run_times) >= 2:
        prev_rt = run_times[1]
        cur.execute("SELECT * FROM rankings WHERE run_time = ?", (prev_rt,))
        result["previous"] = cur.fetchall()

    # history_14_runs
    last_14 = run_times[:14]
    history = []
    for rt in last_14:
        cur.execute("SELECT * FROM rankings WHERE run_time = ?", (rt,))
        rows = cur.fetchall()
        history.append({"run_time": rt, "rankings": rows})
    result["history_14_runs"] = history

    return result


def build_url_changes(conn):
    if conn is None:
        return []

    cur = conn.cursor()

    # Get latest and previous run_times
    cur.execute(
        "SELECT DISTINCT run_time FROM rankings ORDER BY run_time DESC LIMIT 2"
    )
    run_times = [r["run_time"] for r in cur.fetchall()]
    if not run_times:
        return []

    latest_rt = run_times[0]

    # Find rows in latest run where url_changed=1
    cur.execute(
        "SELECT keyword, device, url_path FROM rankings "
        "WHERE run_time = ? AND url_changed = 1",
        (latest_rt,),
    )
    changed_rows = cur.fetchall()

    if not changed_rows or len(run_times) < 2:
        # Return without previous url if no previous run
        return [
            {
                "keyword": r["keyword"],
                "device": r["device"],
                "current_url_path": r["url_path"],
                "previous_url_path": None,
            }
            for r in changed_rows
        ]

    prev_rt = run_times[1]
    changes = []
    for r in changed_rows:
        cur.execute(
            "SELECT url_path FROM rankings "
            "WHERE run_time = ? AND keyword = ? AND device = ?",
            (prev_rt, r["keyword"], r["device"]),
        )
        prev_row = cur.fetchone()
        changes.append(
            {
                "keyword": r["keyword"],
                "device": r["device"],
                "current_url_path": r["url_path"],
                "previous_url_path": prev_row["url_path"] if prev_row else None,
            }
        )
    return changes


def build_discovered_competitors(conn):
    result = {"unalerted": [], "all": []}
    if conn is None:
        return result

    cur = conn.cursor()

    try:
        cur.execute("SELECT * FROM discovered_competitors WHERE alerted = 0")
        result["unalerted"] = cur.fetchall()

        cur.execute("SELECT * FROM discovered_competitors")
        result["all"] = cur.fetchall()
    except sqlite3.OperationalError:
        pass  # table may not exist

    return result


def build_redirect(conn):
    result = {
        "health": [],
        "sightings_latest_run": [],
        "consolidation_by_keyword": [],
        "overall_consolidation_pct": 0,
        "history_30_days": [],
    }
    if conn is None:
        return result

    cur = conn.cursor()

    # --- health: latest per old_domain ---
    try:
        cur.execute(
            "SELECT * FROM redirect_health "
            "WHERE checked_at = ("
            "  SELECT MAX(rh2.checked_at) FROM redirect_health rh2 "
            "  WHERE rh2.old_domain = redirect_health.old_domain"
            ")"
        )
        result["health"] = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # --- sightings_latest_run ---
    try:
        cur.execute("SELECT MAX(run_time) AS rt FROM redirect_sightings")
        row = cur.fetchone()
        latest_rt = row["rt"] if row else None
        if latest_rt:
            cur.execute(
                "SELECT * FROM redirect_sightings WHERE run_time = ?",
                (latest_rt,),
            )
            result["sightings_latest_run"] = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # --- consolidation_by_keyword ---
    try:
        cur.execute("SELECT MAX(run_time) AS rt FROM redirect_sightings")
        row = cur.fetchone()
        latest_rt = row["rt"] if row else None
        if latest_rt:
            cur.execute(
                """
                SELECT
                    keyword,
                    old_domain,
                    MAX(CASE WHEN device='desktop' THEN status END) AS desktop_status,
                    MAX(CASE WHEN device='mobile' THEN status END) AS mobile_status,
                    MAX(CASE WHEN device='desktop' THEN position END) AS desktop_their_position,
                    MAX(CASE WHEN device='mobile' THEN position END) AS mobile_their_position,
                    MAX(CASE WHEN device='desktop' THEN main_domain_position END) AS desktop_my_position,
                    MAX(CASE WHEN device='mobile' THEN main_domain_position END) AS mobile_my_position,
                    MIN(first_seen) AS first_seen,
                    MAX(last_seen) AS last_seen
                FROM (
                    SELECT
                        s.*,
                        (SELECT MIN(s2.run_time) FROM redirect_sightings s2
                         WHERE s2.keyword = s.keyword AND s2.old_domain = s.old_domain) AS first_seen,
                        (SELECT MAX(s2.run_time) FROM redirect_sightings s2
                         WHERE s2.keyword = s.keyword AND s2.old_domain = s.old_domain) AS last_seen
                    FROM redirect_sightings s
                    WHERE s.run_time = ?
                ) sub
                GROUP BY keyword, old_domain
                """,
                (latest_rt,),
            )
            result["consolidation_by_keyword"] = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    # --- overall_consolidation_pct ---
    try:
        cur.execute("SELECT MAX(run_time) AS rt FROM redirect_sightings")
        row = cur.fetchone()
        latest_rt = row["rt"] if row else None
        if latest_rt:
            cur.execute(
                "SELECT COUNT(*) AS total FROM redirect_sightings WHERE run_time = ?",
                (latest_rt,),
            )
            total = cur.fetchone()["total"]
            cur.execute(
                "SELECT COUNT(*) AS consolidated FROM redirect_sightings "
                "WHERE run_time = ? AND status = 'only_main'",
                (latest_rt,),
            )
            consolidated = cur.fetchone()["consolidated"]
            if total > 0:
                result["overall_consolidation_pct"] = round(
                    consolidated / total * 100
                )
    except sqlite3.OperationalError:
        pass

    # --- history_30_days ---
    try:
        thirty_days_ago = (datetime.now(SGT) - timedelta(days=30)).strftime(
            "%Y-%m-%d"
        )
        cur.execute(
            """
            SELECT
                date(run_time) AS date,
                ROUND(
                    CAST(SUM(CASE WHEN status='only_main' THEN 1 ELSE 0 END) AS REAL)
                    / COUNT(*) * 100
                ) AS pct
            FROM redirect_sightings
            WHERE date(run_time) >= ?
            GROUP BY date(run_time)
            ORDER BY date(run_time)
            """,
            (thirty_days_ago,),
        )
        result["history_30_days"] = cur.fetchall()
    except sqlite3.OperationalError:
        pass

    return result


def build_run_log(conn):
    if conn is None:
        return []
    cur = conn.cursor()
    try:
        cur.execute("SELECT * FROM run_log ORDER BY run_time DESC LIMIT 10")
        return cur.fetchall()
    except sqlite3.OperationalError:
        return []


def main():
    config = load_config()
    conn = get_db()

    data = {
        "meta": build_meta(conn, config),
        "rankings": build_rankings(conn),
        "url_changes": build_url_changes(conn),
        "discovered_competitors": build_discovered_competitors(conn),
        "redirect": build_redirect(conn),
        "run_log": build_run_log(conn),
    }

    if conn:
        conn.close()

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"Exported data to {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
