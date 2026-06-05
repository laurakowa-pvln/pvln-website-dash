#!/usr/bin/env python3
"""
Fetches the latest week's data from the Pavilion website analytics Google Sheet
and writes it to data.json. Run daily via GitHub Actions.
"""

import json
import re
import sys
import requests
from datetime import datetime, timezone

SHEET_ID = "1dSUyCL4P1tFQGPkYRSIt35bvJYKvgPB7VrnQ5DWMOj4"
OUTPUT_FILE = "data.json"


def fetch_range(range_str):
    url = (
        f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
        f"/gviz/tq?tqx=out:json&range={range_str}"
    )
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    match = re.search(r"setResponse\(([\s\S]*?)\);\s*$", r.text)
    if not match:
        raise ValueError(f"Could not parse gviz response for range {range_str}")
    data = json.loads(match.group(1))
    rows = []
    for row in data.get("table", {}).get("rows", []) or []:
        cells = []
        for cell in row.get("c", []) or []:
            if cell is None:
                cells.append("")
            else:
                val = cell.get("f")
                if val is None:
                    val = cell.get("v")
                cells.append(str(val) if val is not None else "")
        rows.append(cells)
    return rows


def cell(rows, row_idx, col_idx, default=""):
    try:
        return rows[row_idx][col_idx] or default
    except IndexError:
        return default


def extract_num(text):
    """'12,142 sessions' → '12,142',  '1.3% converted' → '1.3%'"""
    m = re.match(r"^([\d,]+\.?\d*%?)", str(text))
    return m.group(1) if m else str(text)


def extract_delta(text):
    """'↑ 170.1% from last week' → 170.1,  '↓ -18.1% ...' → -18.1"""
    m = re.search(r"([-\d.]+)%", str(text))
    return float(m.group(1)) if m else None


def pct_to_float(text):
    """'188.92%' → 188.92,  '-21.44%' → -21.44,  already a number → passthrough"""
    cleaned = str(text).replace("%", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def main():
    print("Fetching data from Google Sheets...")

    # ── Summary section: B2:N14 ────────────────────────────────────────────
    # Row indices (0-based):
    #   0  → B2  : "Week Starting", C2: date
    #   4  → B6  : "12,142 sessions",  D6: delta text
    #   5  → B7  : "10,377 new users", D7: delta text
    #   6  → B8  : "154 applications", D8: delta text
    #   7  → B9  : "6 ICP applications", D9: delta text
    #   8  → B10 : "1.3% converted",   D10: delta text
    #   11 → B13 : numeric values row  (cols B–N)
    #   12 → B14 : WoW % change row
    summary = fetch_range("B2:N14")

    week_date = cell(summary, 0, 1)

    sessions_val   = extract_num(cell(summary, 4, 0))
    sessions_delta = extract_delta(cell(summary, 4, 2))
    new_users_val  = extract_num(cell(summary, 5, 0))
    new_users_delta = extract_delta(cell(summary, 5, 2))
    apps_val       = extract_num(cell(summary, 6, 0))
    apps_delta     = extract_delta(cell(summary, 6, 2))
    icp_val        = extract_num(cell(summary, 7, 0))
    icp_delta      = extract_delta(cell(summary, 7, 2))
    conv_val       = extract_num(cell(summary, 8, 0))
    conv_delta     = extract_delta(cell(summary, 8, 2))

    # Numeric table row (B13) — col indices from B:
    # J13 = idx 8 (completion rate), L13 = idx 10 (waitlist), N13 = idx 12 (members)
    vals = summary[11] if len(summary) > 11 else []
    wows = summary[12] if len(summary) > 12 else []

    completion_val   = cell(vals, 0, 8) or "—"   # J13
    completion_delta = pct_to_float(cell(wows, 0, 8))  # J14
    waitlist_val     = cell(vals, 0, 10) or "—"  # L13
    waitlist_delta   = pct_to_float(cell(wows, 0, 10))
    members_val      = cell(vals, 0, 12) or "—"  # N13
    members_delta    = pct_to_float(cell(wows, 0, 12))

    # ── Traffic + page data: B19:K32 ───────────────────────────────────────
    # Col indices from B:
    #   0=channel, 1=sessions, 2=sessions WoW%
    #   4=page URL, 5=views, 7=avg duration, 9=engagement rate
    tp = fetch_range("B19:K32")

    traffic = []
    for row in tp:
        if not row or not row[0]:
            continue
        channel  = row[0]
        sessions = row[1] if len(row) > 1 else "—"
        wow      = pct_to_float(row[2]) if len(row) > 2 and row[2] else None
        traffic.append({"channel": channel, "sessions": sessions, "wow": wow})

    pages = []
    for row in tp:
        if not row or len(row) < 5 or not row[4]:
            continue
        url      = row[4]
        views    = row[5] if len(row) > 5 and row[5] else "—"
        duration = row[7] if len(row) > 7 and row[7] else "—"
        eng_rate = row[9] if len(row) > 9 and row[9] else "—"
        # Normalise engagement rate to percentage string
        if eng_rate and eng_rate != "—":
            try:
                eng_rate = f"{float(str(eng_rate).replace('%','')):.0f}%"
            except ValueError:
                pass
        pages.append({"url": url, "views": views, "duration": duration, "engagement_rate": eng_rate})

    updated_at = datetime.now(timezone.utc).strftime("%-d %B %Y")

    data = {
        "week_date":        week_date,
        "updated_at":       updated_at,
        "sessions":         sessions_val,
        "sessions_delta":   sessions_delta,
        "new_users":        new_users_val,
        "new_users_delta":  new_users_delta,
        "apps":             apps_val,
        "apps_delta":       apps_delta,
        "icp":              icp_val,
        "icp_delta":        icp_delta,
        "conversion":       conv_val,
        "conversion_delta": conv_delta,
        "completion":       completion_val,
        "completion_delta": completion_delta,
        "members":          members_val,
        "members_delta":    members_delta,
        "waitlist":         waitlist_val,
        "waitlist_delta":   waitlist_delta,
        "traffic":          traffic,
        "pages":            pages,
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"  Week:        {week_date}")
    print(f"  Sessions:    {sessions_val}  ({sessions_delta}% WoW)")
    print(f"  Applications:{apps_val}  ({apps_delta}% WoW)")
    print(f"  ICP:         {icp_val}  ({icp_delta}% WoW)")
    print(f"  Conversion:  {conv_val}  ({conv_delta}% WoW)")
    print(f"  Members:     {members_val}  ({members_delta}% WoW)")
    print(f"Written to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
