#!/usr/bin/env python3
"""
SEO Rank Tracker - Main script.

Tracks keyword rankings for ibtuition.sg across desktop and mobile,
monitors competitors, discovers new competitors, and checks redirect health.
"""

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from serpapi import GoogleSearch

# ---------------------------------------------------------------------------
# Globals
# ---------------------------------------------------------------------------
DB_PATH = "rankings.db"
CONFIG_PATH = "config.json"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    with open(CONFIG_PATH, "r") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Database initialisation
# ---------------------------------------------------------------------------
def init_db(conn):
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS rankings (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time        TEXT NOT NULL,
            date            TEXT NOT NULL,
            keyword         TEXT NOT NULL,
            device          TEXT NOT NULL,
            domain          TEXT NOT NULL,
            position        INTEGER,
            url             TEXT,
            url_path        TEXT,
            url_changed     INTEGER DEFAULT 0,
            is_my_domain    INTEGER DEFAULT 0,
            is_competitor   INTEGER DEFAULT 0
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS discovered_competitors (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            domain              TEXT UNIQUE NOT NULL,
            first_seen_date     TEXT NOT NULL,
            first_seen_keyword  TEXT NOT NULL,
            first_seen_position INTEGER NOT NULL,
            first_seen_device   TEXT NOT NULL,
            first_seen_url      TEXT NOT NULL,
            alerted             INTEGER DEFAULT 0,
            notes               TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS redirect_sightings (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time                TEXT NOT NULL,
            date                    TEXT NOT NULL,
            keyword                 TEXT NOT NULL,
            device                  TEXT NOT NULL,
            old_domain              TEXT NOT NULL,
            position                INTEGER,
            url                     TEXT,
            main_domain_position    INTEGER,
            cannibalization_gap     INTEGER,
            status                  TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS redirect_health (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            checked_at              TEXT NOT NULL,
            old_domain              TEXT NOT NULL,
            http_status             INTEGER,
            final_destination_url   TEXT,
            redirect_chain          TEXT,
            is_healthy              INTEGER DEFAULT 0,
            error                   TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            run_time            TEXT NOT NULL,
            keywords_checked    INTEGER DEFAULT 0,
            api_calls_used      INTEGER DEFAULT 0,
            errors              TEXT,
            status              TEXT
        )
    """)

    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def extract_domain(url):
    """Return the bare domain (no www.) from a URL."""
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        if domain.startswith("www."):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def extract_path(url):
    """Return just the path portion of a URL."""
    try:
        parsed = urlparse(url)
        return parsed.path or "/"
    except Exception:
        return "/"


def row_exists(conn, table, **kwargs):
    """Check if a row already exists matching all key=value pairs."""
    conditions = " AND ".join(f"{k} = ?" for k in kwargs)
    values = list(kwargs.values())
    cur = conn.execute(
        f"SELECT 1 FROM {table} WHERE {conditions} LIMIT 1", values
    )
    return cur.fetchone() is not None


def get_previous_url_path(conn, keyword, device, domain):
    """Get the most recent ranking url_path for a keyword+device+domain combo."""
    cur = conn.execute(
        """
        SELECT url_path FROM rankings
        WHERE keyword = ? AND device = ? AND domain = ?
        ORDER BY run_time DESC
        LIMIT 1
        """,
        (keyword, device, domain),
    )
    row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# SERP fetching
# ---------------------------------------------------------------------------
def fetch_serp(keyword, device, config):
    """Call SerpApi and return the list of organic results."""
    api_key = os.environ.get("SERPAPI_KEY")
    if not api_key:
        raise RuntimeError("SERPAPI_KEY environment variable is not set")

    params = {
        "engine": "google",
        "q": keyword,
        "gl": config.get("gl", "sg"),
        "hl": config.get("hl", "en"),
        "location": config.get("location", "Singapore"),
        "device": device,
        "num": 100,
        "api_key": api_key,
    }

    search = GoogleSearch(params)
    results = search.get_dict()
    return results.get("organic_results", [])


# ---------------------------------------------------------------------------
# Processing
# ---------------------------------------------------------------------------
def process_serp_results(
    conn, keyword, device, organic_results, config, run_time, today, errors
):
    """
    Process a single SERP response for one keyword+device combination.
    Handles: my domain, known competitors, discovery, redirect sightings.
    """
    my_domain = config["my_domain"]
    known_competitors = [d.lower() for d in config.get("known_competitors", [])]
    redirect_domains_cfg = config.get("redirect_domains", [])
    active_redirect_domains = {
        rd["old_domain"].lower()
        for rd in redirect_domains_cfg
        if rd.get("tracking_active", False)
    }
    all_redirect_domains = {rd["old_domain"].lower() for rd in redirect_domains_cfg}
    top_n = config.get("top_n_for_discovery", 5)
    discovery_enabled = config.get("discovery_enabled", True)

    # Collect positions per domain for cannibalization calculation
    my_domain_positions = []
    redirect_hits = {rd: [] for rd in active_redirect_domains}

    # ------------------------------------------------------------------
    # First pass: gather positions
    # ------------------------------------------------------------------
    for result in organic_results:
        link = result.get("link", "")
        position = result.get("position")
        domain = extract_domain(link)

        if domain == my_domain:
            my_domain_positions.append((position, link))

        for rd in active_redirect_domains:
            if domain == rd:
                redirect_hits[rd].append((position, link))

    # Best (lowest number = highest rank) position for my domain
    my_best_position = None
    if my_domain_positions:
        my_domain_positions.sort(key=lambda x: (x[0] is None, x[0]))
        my_best_position = my_domain_positions[0][0]

    # ------------------------------------------------------------------
    # Second pass: record everything
    # ------------------------------------------------------------------
    previous_path = get_previous_url_path(conn, keyword, device, my_domain)

    for result in organic_results:
        link = result.get("link", "")
        position = result.get("position")
        domain = extract_domain(link)
        path = extract_path(link)

        # --- My domain ---
        if domain == my_domain:
            url_changed = 0
            if previous_path is not None and path != previous_path:
                url_changed = 1

            if not row_exists(
                conn,
                "rankings",
                keyword=keyword,
                device=device,
                domain=domain,
                date=today,
                run_time=run_time,
                position=position,
            ):
                conn.execute(
                    """
                    INSERT INTO rankings
                        (run_time, date, keyword, device, domain, position,
                         url, url_path, url_changed, is_my_domain, is_competitor)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 0)
                    """,
                    (run_time, today, keyword, device, domain, position,
                     link, path, url_changed),
                )

        # --- Known competitors ---
        if domain in known_competitors:
            if not row_exists(
                conn,
                "rankings",
                keyword=keyword,
                device=device,
                domain=domain,
                date=today,
                run_time=run_time,
                position=position,
            ):
                conn.execute(
                    """
                    INSERT INTO rankings
                        (run_time, date, keyword, device, domain, position,
                         url, url_path, url_changed, is_my_domain, is_competitor)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 1)
                    """,
                    (run_time, today, keyword, device, domain, position,
                     link, path),
                )

    # ------------------------------------------------------------------
    # Competitor auto-discovery (top N results only)
    # ------------------------------------------------------------------
    if discovery_enabled:
        for result in organic_results:
            position = result.get("position")
            if position is not None and position > top_n:
                continue
            link = result.get("link", "")
            domain = extract_domain(link)
            if not domain:
                continue
            if domain == my_domain:
                continue
            if domain in known_competitors:
                continue
            if domain in all_redirect_domains:
                continue

            # Insert only if not already discovered
            try:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO discovered_competitors
                        (domain, first_seen_date, first_seen_keyword,
                         first_seen_position, first_seen_device, first_seen_url,
                         alerted)
                    VALUES (?, ?, ?, ?, ?, ?, 0)
                    """,
                    (domain, today, keyword, position, device, link),
                )
            except sqlite3.IntegrityError:
                pass  # already exists

    # ------------------------------------------------------------------
    # Redirect domain SERP sightings
    # ------------------------------------------------------------------
    for rd in active_redirect_domains:
        hits = redirect_hits[rd]
        if hits:
            for pos, url in hits:
                # Determine cannibalization status
                if my_best_position is not None and pos is not None:
                    if pos < my_best_position:
                        status = "outranking"
                    else:
                        status = "both_present"
                    gap = pos - my_best_position
                elif my_best_position is not None:
                    status = "both_present"
                    gap = None
                else:
                    status = "neither"  # shouldn't happen if hit exists but my domain missing
                    gap = None

                # Refine: if redirect found but my domain not found
                if not my_domain_positions and hits:
                    status = "only_redirect"
                    gap = None

                if not row_exists(
                    conn,
                    "redirect_sightings",
                    keyword=keyword,
                    device=device,
                    old_domain=rd,
                    date=today,
                    run_time=run_time,
                ):
                    conn.execute(
                        """
                        INSERT INTO redirect_sightings
                            (run_time, date, keyword, device, old_domain,
                             position, url, main_domain_position,
                             cannibalization_gap, status)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_time, today, keyword, device, rd,
                         pos, url, my_best_position, gap, status),
                    )
        else:
            # Redirect domain not found in results
            if my_domain_positions:
                status = "only_main"
            else:
                status = "neither"

            if not row_exists(
                conn,
                "redirect_sightings",
                keyword=keyword,
                device=device,
                old_domain=rd,
                date=today,
                run_time=run_time,
            ):
                conn.execute(
                    """
                    INSERT INTO redirect_sightings
                        (run_time, date, keyword, device, old_domain,
                         position, url, main_domain_position,
                         cannibalization_gap, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (run_time, today, keyword, device, rd,
                     None, None, my_best_position, None, status),
                )

    conn.commit()


# ---------------------------------------------------------------------------
# 301 redirect health check
# ---------------------------------------------------------------------------
def check_redirect_health(conn, config, run_time):
    """HTTP-check each active redirect domain and record results."""
    redirect_domains_cfg = config.get("redirect_domains", [])
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    for rd_cfg in redirect_domains_cfg:
        if not rd_cfg.get("tracking_active", False):
            continue

        old_domain = rd_cfg["old_domain"]
        target_domain = rd_cfg.get("redirects_to", config["my_domain"])
        check_url = f"https://{old_domain}/"

        http_status = None
        final_url = None
        redirect_chain = []
        is_healthy = 0
        error_msg = None

        try:
            resp = requests.get(check_url, allow_redirects=True, timeout=10)
            http_status = resp.history[0].status_code if resp.history else resp.status_code

            # Build redirect chain
            for r in resp.history:
                redirect_chain.append({
                    "url": r.url,
                    "status": r.status_code,
                })
            redirect_chain.append({
                "url": resp.url,
                "status": resp.status_code,
            })

            final_url = resp.url

            # Healthy only if first hop is 301 and final URL contains target domain
            first_status = resp.history[0].status_code if resp.history else resp.status_code
            if first_status == 301 and target_domain in final_url:
                is_healthy = 1

        except Exception as e:
            error_msg = str(e)

        conn.execute(
            """
            INSERT INTO redirect_health
                (checked_at, old_domain, http_status, final_destination_url,
                 redirect_chain, is_healthy, error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checked_at,
                old_domain,
                http_status,
                final_url,
                json.dumps(redirect_chain),
                is_healthy,
                error_msg,
            ),
        )

    conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    config = load_config()
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    keywords = config.get("keywords", [])
    devices = config.get("devices", ["desktop", "mobile"])

    keywords_checked = 0
    api_calls_used = 0
    errors = []

    for keyword in keywords:
        for device in devices:
            try:
                print(f"[{device}] Fetching: {keyword}")
                organic_results = fetch_serp(keyword, device, config)
                api_calls_used += 1

                process_serp_results(
                    conn, keyword, device, organic_results,
                    config, run_time, today, errors,
                )
                keywords_checked += 1

            except Exception as e:
                error_detail = f"{keyword} ({device}): {e}"
                print(f"  ERROR: {error_detail}")
                errors.append(error_detail)
                continue

    # 301 health checks
    try:
        print("Running redirect health checks...")
        check_redirect_health(conn, config, run_time)
    except Exception as e:
        error_detail = f"redirect_health: {e}"
        print(f"  ERROR: {error_detail}")
        errors.append(error_detail)

    # Run log
    status = "ok" if not errors else "partial_error"
    conn.execute(
        """
        INSERT INTO run_log
            (run_time, keywords_checked, api_calls_used, errors, status)
        VALUES (?, ?, ?, ?, ?)
        """,
        (run_time, keywords_checked, api_calls_used, json.dumps(errors), status),
    )
    conn.commit()

    print(f"\nRun complete: {keywords_checked} keyword+device combos checked, "
          f"{api_calls_used} API calls, {len(errors)} error(s).")

    conn.close()


if __name__ == "__main__":
    main()
