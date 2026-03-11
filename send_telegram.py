#!/usr/bin/env python3
"""
send_telegram.py — Send a Telegram SEO report after each tracker run.

Env vars required:
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID

Reads from:
    rankings.db  (SQLite)
    config.json
"""

import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SGT = timezone(timedelta(hours=8))
TELEGRAM_MAX_LEN = 4096
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rankings.db")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


def dict_factory(cursor, row):
    return {col[0]: row[i] for i, col in enumerate(cursor.description)}


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = dict_factory
    return conn


def truncate_path(url_path, max_len=25):
    """Shorten a URL path for display. '/' becomes 'Home'."""
    if not url_path or url_path == "/":
        return "Home"
    path = url_path[:max_len]
    if len(url_path) > max_len:
        path = path.rstrip("/") + "..."
    return path


def arrow(change):
    """Return ↑N / ↓N / '' for a position change (positive = improvement)."""
    if change is None or change == 0:
        return ""
    if change > 0:
        return f" \u2191{change}"
    return f" \u2193{abs(change)}"


def device_icon(device):
    return "\U0001f5a5" if device == "desktop" else "\U0001f4f1"


# ---------------------------------------------------------------------------
# Data queries (all use the schema from tracker.py)
# ---------------------------------------------------------------------------

def get_latest_run_time(db):
    row = db.execute(
        "SELECT run_time FROM run_log ORDER BY run_time DESC LIMIT 1"
    ).fetchone()
    return row["run_time"] if row else None


def get_previous_run_time(db, current_run_time):
    row = db.execute(
        "SELECT DISTINCT run_time FROM rankings WHERE run_time < ? "
        "ORDER BY run_time DESC LIMIT 1",
        (current_run_time,),
    ).fetchone()
    return row["run_time"] if row else None


def get_run_count_today(db):
    today_str = datetime.now(SGT).strftime("%Y-%m-%d")
    row = db.execute(
        "SELECT COUNT(DISTINCT run_time) AS cnt FROM run_log "
        "WHERE date(run_time) = ?",
        (today_str,),
    ).fetchone()
    return row["cnt"] if row else 0


def get_api_usage_this_month(db):
    month_str = datetime.now(SGT).strftime("%Y-%m")
    row = db.execute(
        "SELECT COALESCE(SUM(api_calls_used), 0) AS total FROM run_log "
        "WHERE strftime('%Y-%m', run_time) = ?",
        (month_str,),
    ).fetchone()
    return row["total"] if row else 0


def get_my_rankings(db, run_time):
    """Return my domain rankings for a given run_time."""
    rows = db.execute(
        "SELECT keyword, device, position, url_path, url_changed "
        "FROM rankings WHERE run_time = ? AND is_my_domain = 1",
        (run_time,),
    ).fetchall()
    return rows


def get_competitor_rankings(db, run_time):
    """Return competitor rankings for a given run_time."""
    rows = db.execute(
        "SELECT keyword, device, domain, position, url "
        "FROM rankings WHERE run_time = ? AND is_competitor = 1",
        (run_time,),
    ).fetchall()
    return rows


def get_redirect_health(db):
    """Return latest redirect_health per old_domain."""
    rows = db.execute(
        "SELECT rh.* FROM redirect_health rh "
        "INNER JOIN ("
        "  SELECT old_domain, MAX(checked_at) AS max_at "
        "  FROM redirect_health GROUP BY old_domain"
        ") latest ON rh.old_domain = latest.old_domain "
        "AND rh.checked_at = latest.max_at"
    ).fetchall()
    return rows


def get_redirect_sightings(db, run_time):
    """Return redirect sightings for a given run_time."""
    rows = db.execute(
        "SELECT keyword, device, old_domain, position, url, "
        "main_domain_position, cannibalization_gap, status "
        "FROM redirect_sightings WHERE run_time = ?",
        (run_time,),
    ).fetchall()
    return rows


def get_new_competitors(db):
    """Return unalerted discovered competitors."""
    rows = db.execute(
        "SELECT id, domain, first_seen_keyword, first_seen_device, "
        "first_seen_position, first_seen_url "
        "FROM discovered_competitors WHERE alerted = 0"
    ).fetchall()
    return rows


def mark_competitors_alerted(db, ids):
    if not ids:
        return
    placeholders = ",".join("?" for _ in ids)
    db.execute(
        f"UPDATE discovered_competitors SET alerted = 1 "
        f"WHERE id IN ({placeholders})",
        ids,
    )
    db.commit()


# ---------------------------------------------------------------------------
# Message builder
# ---------------------------------------------------------------------------

def build_message(db, config):
    parts = []
    now = datetime.now(SGT)

    latest_rt = get_latest_run_time(db)
    if not latest_rt:
        return "No run data found in rankings.db."

    prev_rt = get_previous_run_time(db, latest_rt)
    my_rankings = get_my_rankings(db, latest_rt)
    prev_rankings = get_my_rankings(db, prev_rt) if prev_rt else []
    competitor_rankings = get_competitor_rankings(db, latest_rt)
    health_rows = get_redirect_health(db)
    sighting_rows = get_redirect_sightings(db, latest_rt)
    new_competitors = get_new_competitors(db)
    run_count = get_run_count_today(db)
    api_used = get_api_usage_this_month(db)
    api_limit = config.get("serpapi_monthly_limit", 100)
    my_domain = config.get("my_domain", "ibtuition.sg")
    dashboard_url = config.get("github_pages_url", "")

    # Build previous rankings lookup: (keyword, device) -> row
    prev_map = {(r["keyword"], r["device"]): r for r in prev_rankings}

    has_changes = False

    # ------------------------------------------------------------------
    # 0. REDIRECT HEALTH FAILURES (always at very top)
    # ------------------------------------------------------------------
    unhealthy = [r for r in health_rows if not r["is_healthy"]]
    if unhealthy:
        has_changes = True
        lines = ["\U0001f534 REDIRECT HEALTH FAILURE"]
        for r in unhealthy:
            status = r.get("http_status") or "N/A"
            err = r.get("error") or ""
            lines.append(
                f'{r["old_domain"]} \u2192 HTTP {status} \u274c'
                + (f" ({err})" if err else "")
            )
        parts.append("\n".join(lines))

    # ------------------------------------------------------------------
    # Header
    # ------------------------------------------------------------------
    header = (
        f"\U0001f4ca SEO Report \u2014 {now.strftime('%d %b %Y')} "
        f"{now.strftime('%H:%M')} SGT\n"
        f"\U0001f4cd Google Singapore | \U0001f5a5 Desktop & \U0001f4f1 Mobile "
        f"(tracked separately)\n"
        f"\U0001f504 Run {run_count} of 3 today"
    )
    parts.append(header)

    # ------------------------------------------------------------------
    # 1. Rankings table
    # ------------------------------------------------------------------
    # Group by keyword: {keyword: {device: row}}
    kw_data = {}
    for r in my_rankings:
        kw_data.setdefault(r["keyword"], {})[r["device"]] = r

    ranking_lines = [
        f"\U0001f3c6 {my_domain} Rankings",
        "\u2501" * 25,
    ]
    for kw in sorted(kw_data.keys()):
        devices = kw_data[kw]
        cols = []
        for dev in ("desktop", "mobile"):
            info = devices.get(dev)
            if info and info["position"]:
                prev = prev_map.get((kw, dev))
                change = 0
                if prev and prev["position"]:
                    change = prev["position"] - info["position"]
                pos_str = f'#{info["position"]}'
                chg_str = arrow(change)
                path_str = truncate_path(info.get("url_path", "/"))
                cols.append(f"{device_icon(dev)} {pos_str}{chg_str} {path_str}")
            else:
                cols.append(f"{device_icon(dev)} \u2014")
        line = f"{kw}  {'  '.join(cols)}"
        ranking_lines.append(line)

    parts.append("\n".join(ranking_lines))

    # ------------------------------------------------------------------
    # 2. Page changes
    # ------------------------------------------------------------------
    page_change_lines = []
    for r in my_rankings:
        if r.get("url_changed"):
            prev = prev_map.get((r["keyword"], r["device"]))
            old_path = truncate_path(prev["url_path"]) if prev else "?"
            new_path = truncate_path(r["url_path"])
            page_change_lines.append(
                f'"{r["keyword"]}" {device_icon(r["device"])} '
                f"was {old_path} \u2192 now {new_path}"
            )
    if page_change_lines:
        has_changes = True
        section = "\u26a0\ufe0f Ranking Page Changes\n" + "\n".join(page_change_lines)
        section += "\n\u26a0\ufe0f Possible keyword cannibalization \u2014 check GSC"
        parts.append(section)

    # ------------------------------------------------------------------
    # 3. Competitor comparison
    # ------------------------------------------------------------------
    comp_best = {}  # (keyword, device) -> {domain, position}
    for cr in competitor_rankings:
        key = (cr["keyword"], cr["device"])
        if cr["position"] is not None:
            if key not in comp_best or cr["position"] < comp_best[key]["position"]:
                comp_best[key] = cr

    beating_all = []
    losing_lines = []
    for kw in sorted(kw_data.keys()):
        for dev in ("desktop", "mobile"):
            my_info = kw_data[kw].get(dev)
            if not my_info or not my_info["position"]:
                continue
            best_comp = comp_best.get((kw, dev))
            if not best_comp:
                beating_all.append(kw)
                continue
            if my_info["position"] <= best_comp["position"]:
                beating_all.append(kw)
            else:
                has_changes = True
                losing_lines.append(
                    f'\u26a0\ufe0f Losing to competitor: {kw}\n'
                    f'  \u2192 {best_comp["domain"]} at #{best_comp["position"]} '
                    f'(you are #{my_info["position"]})'
                )

    comp_parts = []
    beating_unique = list(dict.fromkeys(beating_all))
    if beating_unique:
        comp_parts.append(
            "\u2705 Beating all competitors: " + ", ".join(beating_unique)
        )
    if losing_lines:
        comp_parts.extend(losing_lines)
    if comp_parts:
        parts.append("\n".join(comp_parts))

    # ------------------------------------------------------------------
    # 4. New competitors
    # ------------------------------------------------------------------
    if new_competitors:
        has_changes = True
        nc_lines = ["\U0001f195 NEW COMPETITOR DETECTED"]
        for nc in new_competitors:
            dev_label = nc["first_seen_device"]
            nc_lines.append(
                f'{nc["domain"]} appeared at #{nc["first_seen_position"]} '
                f'for "{nc["first_seen_keyword"]}" '
                f'{device_icon(nc["first_seen_device"])} {dev_label}'
            )
            if nc.get("first_seen_url"):
                nc_lines.append(f'URL: {nc["first_seen_url"]}')
        nc_lines.append("\u2192 Add to config.json to track going forward")
        parts.append("\n".join(nc_lines))

    # ------------------------------------------------------------------
    # 5. Redirect status
    # ------------------------------------------------------------------
    redirect_domains = config.get("redirect_domains", [])
    active_redirects = [
        rd for rd in redirect_domains if rd.get("tracking_active", False)
    ]

    if active_redirects:
        # Consolidation stats from sightings
        total_sightings = len(sighting_rows) if sighting_rows else 0
        clean_count = sum(
            1 for s in sighting_rows if s["status"] == "only_main"
        )
        pct = round(clean_count / total_sightings * 100) if total_sightings else 100

        still_in_serps = [
            s for s in sighting_rows
            if s["status"] in ("outranking", "both_present")
        ]

        if pct >= 100 and not still_in_serps and not unhealthy:
            parts.append(
                "\U0001f500 Redirects: \u2705 All clean (100% consolidated)"
            )
        else:
            redir_lines = [
                "\U0001f500 Redirect Status",
                "\u2501" * 25,
                f"Consolidation: {pct}% complete "
                f"({clean_count}/{total_sightings} keywords clean)",
            ]
            for rd in active_redirects:
                dom = rd["old_domain"]
                check = next(
                    (r for r in health_rows if r["old_domain"] == dom), None
                )
                if check:
                    if check["is_healthy"]:
                        redir_lines.append(f"{dom} \u2192 \u2705 301 healthy")
                    else:
                        redir_lines.append(
                            f'{dom} \u2192 \u274c HTTP {check.get("http_status", "?")}'
                        )
                else:
                    redir_lines.append(f"{dom} \u2192 \u2753 no data")

            if still_in_serps:
                has_changes = True
                redir_lines.append("")
                redir_lines.append("\u26a0\ufe0f Still in SERPs:")
                for sa in still_in_serps:
                    my_pos = sa["main_domain_position"] or "\u2014"
                    old_pos = sa["position"] or "\u2014"
                    if sa["status"] == "outranking":
                        indicator = "\U0001f534 outranking you"
                    else:
                        indicator = "\U0001f7e1 both present"
                    redir_lines.append(
                        f'"{sa["keyword"]}" \u2014 {sa["old_domain"]} '
                        f"#{old_pos} vs your #{my_pos} {indicator}"
                    )
            parts.append("\n".join(redir_lines))

    # ------------------------------------------------------------------
    # 6. Biggest moves
    # ------------------------------------------------------------------
    moves = []
    for r in my_rankings:
        if not r["position"]:
            continue
        prev = prev_map.get((r["keyword"], r["device"]))
        if prev and prev["position"]:
            diff = prev["position"] - r["position"]
            if diff != 0:
                has_changes = True
                moves.append({
                    "keyword": r["keyword"],
                    "device": r["device"],
                    "change": diff,
                    "abs_change": abs(diff),
                })
    moves.sort(key=lambda m: m["abs_change"], reverse=True)
    top_moves = moves[:3]
    if top_moves:
        move_lines = ["\U0001f4c8 Biggest Moves This Run"]
        for m in top_moves:
            chg = arrow(m["change"]).strip()
            move_lines.append(
                f'"{m["keyword"]}" {chg} '
                f'{device_icon(m["device"])} {m["device"]}'
            )
        parts.append("\n".join(move_lines))

    # ------------------------------------------------------------------
    # 7. "No changes" fallback
    # ------------------------------------------------------------------
    if not has_changes and prev_rt is not None:
        parts.append("No changes this run \U0001f3af")

    # ------------------------------------------------------------------
    # Footer
    # ------------------------------------------------------------------
    footer_lines = [
        f"\U0001f4b3 API: {api_used}/{api_limit} credits used this month",
    ]
    if dashboard_url:
        footer_lines.append(f"\U0001f517 Dashboard: {dashboard_url}")
    parts.append("\n".join(footer_lines))

    # ------------------------------------------------------------------
    # Assemble & truncate
    # ------------------------------------------------------------------
    message = "\n\n".join(parts)

    if len(message) > TELEGRAM_MAX_LEN:
        while len(message) > TELEGRAM_MAX_LEN - 50 and len(ranking_lines) > 3:
            ranking_lines.pop(-1)
        ranking_lines.append("... (truncated)")
        parts_new = []
        for p in parts:
            if p.startswith("\U0001f3c6"):
                parts_new.append("\n".join(ranking_lines))
            else:
                parts_new.append(p)
        parts = parts_new
        message = "\n\n".join(parts)

    if len(message) > TELEGRAM_MAX_LEN:
        message = message[: TELEGRAM_MAX_LEN - 20] + "\n... (truncated)"

    return message


# ---------------------------------------------------------------------------
# Telegram sender
# ---------------------------------------------------------------------------

def send_telegram(message, token, chat_id):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
    }
    resp = requests.post(url, json=payload, timeout=30)

    # If Markdown parsing fails, retry without parse_mode
    if not resp.ok and "can't parse entities" in resp.text.lower():
        payload.pop("parse_mode")
        resp = requests.post(url, json=payload, timeout=30)

    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        print(f"ERROR: {DB_PATH} not found.")
        sys.exit(1)

    config = load_config()
    db = get_db()

    try:
        message = build_message(db, config)
        print("--- Telegram message ---")
        print(message)
        print("--- End ---")
        result = send_telegram(message, token, chat_id)

        # Mark new competitors as alerted after successful send
        new_comps = get_new_competitors(db)
        if new_comps:
            mark_competitors_alerted(db, [c["id"] for c in new_comps])

        if result.get("ok"):
            print("Telegram message sent successfully.")
        else:
            print(f"Telegram API returned: {result}")
            sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
