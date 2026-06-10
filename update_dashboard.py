#!/usr/bin/env python3
"""
Fetches weekly GA4 data from raw Google Sheet tabs and writes data.json.
HubSpot metrics (apps, ICP, completion, members, waitlist) require a separate
HubSpot Private App token — set HUBSPOT_TOKEN env var to enable auto-pull,
otherwise existing values in data.json are preserved.
"""

import json
import os
import re
import sys
import requests
from datetime import date, timedelta, timezone, datetime

SHEET_ID = "1dSUyCL4P1tFQGPkYRSIt35bvJYKvgPB7VrnQ5DWMOj4"
TRAFFIC_GID = 351180145   # raw daily traffic-by-channel tab
OUTPUT_FILE = "data.json"


# ── Date helpers ──────────────────────────────────────────────────────────────

def last_complete_week():
    """Returns (start, end) of the most recently completed Sun–Sat week."""
    today = date.today()
    # weekday(): Mon=0 … Sat=5, Sun=6
    # days since last Saturday (if today IS Saturday, use last Saturday = 7 days ago)
    days_since_sat = (today.weekday() - 5) % 7 or 7
    end = today - timedelta(days=days_since_sat)    # last Saturday
    start = end - timedelta(days=6)                 # preceding Sunday
    return start, end


def prev_week(start, end):
    return start - timedelta(days=7), end - timedelta(days=7)


# ── Sheet fetcher ─────────────────────────────────────────────────────────────

def fetch_gid(gid):
    url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:json&gid={gid}"
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    match = re.search(r"setResponse\(([\s\S]*?)\);\s*$", r.text)
    if not match:
        raise ValueError(f"Could not parse gviz response for gid {gid}")
    data = json.loads(match.group(1))
    rows = []
    for row in data.get("table", {}).get("rows", []) or []:
        cells = []
        for cell in row.get("c", []) or []:
            if cell is None:
                cells.append("")
            else:
                val = cell.get("f") or cell.get("v")
                cells.append(str(val) if val is not None else "")
        rows.append(cells)
    return rows


def fetch_sheet_name(sheet_name):
    import urllib.parse
    url = (f"https://docs.google.com/spreadsheets/d/{SHEET_ID}"
           f"/gviz/tq?tqx=out:json&sheet={urllib.parse.quote(sheet_name)}")
    r = requests.get(url, timeout=20)
    r.raise_for_status()
    match = re.search(r"setResponse\(([\s\S]*?)\);\s*$", r.text)
    if not match:
        return []
    data = json.loads(match.group(1))
    rows = []
    for row in data.get("table", {}).get("rows", []) or []:
        cells = []
        for cell in row.get("c", []) or []:
            if cell is None:
                cells.append("")
            else:
                val = cell.get("f") or cell.get("v")
                cells.append(str(val) if val is not None else "")
        rows.append(cells)
    return rows


# ── Parsers ───────────────────────────────────────────────────────────────────

def parse_date(s):
    try:
        parts = s.strip().split('/')
        if len(parts) == 3:
            return date(int(parts[2]), int(parts[0]), int(parts[1]))
    except Exception:
        pass
    try:
        return date.fromisoformat(s.strip()[:10])
    except Exception:
        return None


def to_int(s):
    try:
        return int(str(s).replace(',', '').split('.')[0])
    except Exception:
        return 0


def wow_pct(this, prev):
    if not prev:
        return None
    return round((this - prev) / prev * 100, 1)


# ── GA4: sessions + new users from user health_raw ───────────────────────────

def get_ga4_health(week_start, week_end, prev_start, prev_end):
    rows = fetch_sheet_name("user health_raw")
    # Columns (0-based after date): [sessions, users, engaged, new_users, other]
    # Confirmed: col[4] = new_users (matched historical data)
    totals = {
        'sessions': {True: 0, False: 0},
        'new_users': {True: 0, False: 0},
    }
    for row in rows:
        if not row:
            continue
        d = parse_date(row[0])
        if not d:
            continue
        is_this = week_start <= d <= week_end
        is_prev = prev_start <= d <= prev_end
        if not is_this and not is_prev:
            continue
        sessions = to_int(row[1]) if len(row) > 1 else 0
        new_users = to_int(row[4]) if len(row) > 4 else 0
        flag = is_this
        totals['sessions'][flag] += sessions
        totals['new_users'][flag] += new_users
    return (
        totals['sessions'][True],  totals['sessions'][False],
        totals['new_users'][True], totals['new_users'][False],
    )


# ── GA4: traffic by channel ───────────────────────────────────────────────────

def get_traffic(week_start, week_end, prev_start, prev_end):
    rows = fetch_gid(TRAFFIC_GID)
    # Columns: channel, date, sessions, ...
    by_channel = {}
    for row in rows:
        if len(row) < 3:
            continue
        channel = row[0].strip()
        d = parse_date(row[1])
        if not d or not channel:
            continue
        sessions = to_int(row[2])
        if week_start <= d <= week_end:
            by_channel.setdefault(channel, {'a': 0, 'b': 0})['a'] += sessions
        elif prev_start <= d <= prev_end:
            by_channel.setdefault(channel, {'a': 0, 'b': 0})['b'] += sessions

    result = []
    for ch, v in sorted(by_channel.items(), key=lambda x: -x[1]['a']):
        if v['a'] == 0 and v['b'] == 0:
            continue
        result.append({
            "channel": ch,
            "sessions": f"{v['a']:,}",
            "wow": wow_pct(v['a'], v['b']),
        })
    return result


# ── HubSpot (optional — requires HUBSPOT_TOKEN env var) ───────────────────────

def get_hubspot_metrics(week_start, week_end, prev_start, prev_end, existing):
    token = os.environ.get("HUBSPOT_TOKEN", "")
    if not token:
        print("  HUBSPOT_TOKEN not set — preserving existing HubSpot values.")
        return {k: existing.get(k) for k in [
            'apps', 'apps_delta', 'icp', 'icp_delta',
            'conversion', 'conversion_delta',
            'completion', 'completion_delta',
            'members', 'members_delta',
            'waitlist', 'waitlist_delta',
        ]}

    def hs_count(filter_groups):
        url = "https://api.hubapi.com/crm/v3/objects/contacts/search"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        body = {"filterGroups": filter_groups, "limit": 1, "properties": ["hs_object_id"]}
        r = requests.post(url, json=body, headers=headers, timeout=15)
        r.raise_for_status()
        return r.json().get("total", 0)

    def date_filters(prop, start, end):
        return [{"filters": [
            {"propertyName": prop, "operator": "GTE", "value": str(start)},
            {"propertyName": prop, "operator": "LTE", "value": str(end)},
        ]}]

    print("  Fetching HubSpot metrics...")
    apps_a  = hs_count(date_filters("step_1_completed_date", week_start, week_end))
    apps_b  = hs_count(date_filters("step_1_completed_date", prev_start, prev_end))
    step2_a = hs_count(date_filters("step_2_completed_date", week_start, week_end))
    step2_b = hs_count(date_filters("step_2_completed_date", prev_start, prev_end))

    icp_filters_a = [{"filters": [
        {"propertyName": "step_1_completed_date", "operator": "GTE", "value": str(week_start)},
        {"propertyName": "step_1_completed_date", "operator": "LTE", "value": str(week_end)},
        {"propertyName": "pavilion_icp",          "operator": "EQ",  "value": "true"},
    ]}]
    icp_filters_b = [{"filters": [
        {"propertyName": "step_1_completed_date", "operator": "GTE", "value": str(prev_start)},
        {"propertyName": "step_1_completed_date", "operator": "LTE", "value": str(prev_end)},
        {"propertyName": "pavilion_icp",          "operator": "EQ",  "value": "true"},
    ]}]
    icp_a = hs_count(icp_filters_a)
    icp_b = hs_count(icp_filters_b)

    mem_a = hs_count(date_filters("membership_start_date", week_start, week_end))
    mem_b = hs_count(date_filters("membership_start_date", prev_start, prev_end))
    wl_a  = hs_count(date_filters("date_added_to_waitlist", week_start, week_end))
    wl_b  = hs_count(date_filters("date_added_to_waitlist", prev_start, prev_end))

    comp_a = round(step2_a / apps_a * 100, 1) if apps_a else 0
    comp_b = round(step2_b / apps_b * 100, 1) if apps_b else 0

    return {
        'apps':              str(apps_a),
        'apps_delta':        wow_pct(apps_a, apps_b),
        'icp':               str(icp_a),
        'icp_delta':         wow_pct(icp_a, icp_b),
        'completion':        f"{comp_a}%",
        'completion_delta':  wow_pct(comp_a, comp_b),
        'members':           str(mem_a),
        'members_delta':     wow_pct(mem_a, mem_b),
        'waitlist':          str(wl_a),
        'waitlist_delta':    wow_pct(wl_a, wl_b),
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    week_start, week_end = last_complete_week()
    prev_start, prev_end = prev_week(week_start, week_end)

    print(f"Week:      {week_start} → {week_end}")
    print(f"Prev week: {prev_start} → {prev_end}")

    # Load existing data.json to preserve any values we can't fetch
    existing = {}
    try:
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
    except Exception:
        pass

    print("Fetching GA4 health metrics...")
    sessions_a, sessions_b, new_users_a, new_users_b = get_ga4_health(
        week_start, week_end, prev_start, prev_end
    )

    print("Fetching GA4 traffic by channel...")
    traffic = get_traffic(week_start, week_end, prev_start, prev_end)

    print("Fetching HubSpot metrics...")
    hs = get_hubspot_metrics(week_start, week_end, prev_start, prev_end, existing)

    sessions_conv_a = to_int(str(hs.get('apps', '0')).replace(',', ''))
    sessions_conv_b_val = to_int(str(existing.get('apps', '0')).replace(',', ''))
    conv_a = round(sessions_conv_a / sessions_a * 100, 1) if sessions_a else 0
    conv_b = round(sessions_conv_b_val / sessions_b * 100, 1) if sessions_b and sessions_conv_b_val else None

    # If HubSpot token available, recalculate conversion with fresh data
    if os.environ.get("HUBSPOT_TOKEN"):
        hs['conversion'] = f"{conv_a}%"
        hs['conversion_delta'] = wow_pct(conv_a, conv_b) if conv_b else None

    updated_at = datetime.now(timezone.utc).strftime("%-d %B %Y")
    week_label = week_start.strftime("%-m/%-d/%Y")

    data = {
        "week_date":        week_label,
        "updated_at":       updated_at,
        "sessions":         f"{sessions_a:,}",
        "sessions_delta":   wow_pct(sessions_a, sessions_b),
        "new_users":        f"{new_users_a:,}",
        "new_users_delta":  wow_pct(new_users_a, new_users_b),
        **hs,
        "traffic":          traffic,
        "pages":            existing.get("pages", []),
    }

    with open(OUTPUT_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\n  Sessions:   {sessions_a:,}  ({data['sessions_delta']}% WoW)")
    print(f"  New users:  {new_users_a:,}  ({data['new_users_delta']}% WoW)")
    print(f"  Apps:       {hs.get('apps')}  ({hs.get('apps_delta')}% WoW)")
    print(f"  ICP:        {hs.get('icp')}  ({hs.get('icp_delta')}% WoW)")
    print(f"  Conversion: {hs.get('conversion')}  ({hs.get('conversion_delta')}% WoW)")
    print(f"  Completion: {hs.get('completion')}  ({hs.get('completion_delta')}% WoW)")
    print(f"  Members:    {hs.get('members')}  ({hs.get('members_delta')}% WoW)")
    print(f"  Waitlist:   {hs.get('waitlist')}  ({hs.get('waitlist_delta')}% WoW)")
    print(f"\nWritten to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
