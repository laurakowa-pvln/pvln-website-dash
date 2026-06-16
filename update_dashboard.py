#!/usr/bin/env python3
"""
Fetches weekly GA4 data directly via the Analytics Data API and HubSpot CRM,
then writes data.json for the GitHub Pages dashboard.
Requires: GA4_SERVICE_ACCOUNT_JSON and HUBSPOT_PAK env vars.
"""

import json
import os
import sys
import requests
from datetime import date, timedelta, timezone, datetime

GA4_PROPERTY_ID = "348455384"
OUTPUT_FILE = "data.json"


# ── Date helpers ──────────────────────────────────────────────────────────────

def last_complete_week():
    """Returns (start, end) of the most recently completed Sun–Sat week."""
    today = date.today()
    days_since_sat = (today.weekday() - 5) % 7 or 7
    end = today - timedelta(days=days_since_sat)
    start = end - timedelta(days=6)
    return start, end


def prev_week(start, end):
    return start - timedelta(days=7), end - timedelta(days=7)


def wow_pct(this, prev):
    if not prev:
        return None
    return round((this - prev) / prev * 100, 1)


def to_int(s):
    try:
        return int(str(s).replace(',', '').split('.')[0])
    except Exception:
        return 0


# ── GA4 ───────────────────────────────────────────────────────────────────────

def get_ga4_token():
    sa_json = os.environ.get('GA4_SERVICE_ACCOUNT_JSON', '')
    if not sa_json:
        raise ValueError('GA4_SERVICE_ACCOUNT_JSON env var not set')
    from google.oauth2 import service_account
    import google.auth.transport.requests
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=['https://www.googleapis.com/auth/analytics.readonly'])
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def ga4_report(token, body):
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PROPERTY_ID}:runReport"
    r = requests.post(url, json=body,
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return r.json()


def get_ga4_health(token, week_start, week_end, prev_start, prev_end):
    data = ga4_report(token, {
        "dateRanges": [
            {"startDate": str(week_start), "endDate": str(week_end), "name": "this_week"},
            {"startDate": str(prev_start), "endDate": str(prev_end), "name": "prev_week"},
        ],
        "metrics": [{"name": "sessions"}, {"name": "newUsers"}],
    })
    result = {"this_week": (0, 0), "prev_week": (0, 0)}
    for row in data.get("rows", []):
        rng = row["dimensionValues"][0]["value"]
        sessions  = int(row["metricValues"][0]["value"])
        new_users = int(row["metricValues"][1]["value"])
        result[rng] = (sessions, new_users)
    tw, pw = result["this_week"], result["prev_week"]
    return tw[0], pw[0], tw[1], pw[1]


def get_traffic(token, week_start, week_end, prev_start, prev_end):
    data = ga4_report(token, {
        "dateRanges": [
            {"startDate": str(week_start), "endDate": str(week_end), "name": "this_week"},
            {"startDate": str(prev_start), "endDate": str(prev_end), "name": "prev_week"},
        ],
        "dimensions": [{"name": "sessionDefaultChannelGroup"}],
        "metrics": [{"name": "sessions"}],
    })
    by_channel = {}
    for row in data.get("rows", []):
        channel  = row["dimensionValues"][0]["value"]
        rng      = row["dimensionValues"][1]["value"]
        sessions = int(row["metricValues"][0]["value"])
        by_channel.setdefault(channel, {"this_week": 0, "prev_week": 0})[rng] += sessions

    return [
        {"channel": ch, "sessions": f"{v['this_week']:,}", "wow": wow_pct(v["this_week"], v["prev_week"])}
        for ch, v in sorted(by_channel.items(), key=lambda x: -x[1]["this_week"])
        if v["this_week"] or v["prev_week"]
    ]


# ── HubSpot ───────────────────────────────────────────────────────────────────

def get_hubspot_token():
    token = os.environ.get("HUBSPOT_TOKEN", "")
    if token:
        return token
    pak = os.environ.get("HUBSPOT_PAK", "")
    if not pak:
        return None
    portal_id = os.environ.get("HUBSPOT_PORTAL_ID", "5242563")
    r = requests.post(
        f"https://api.hubspot.com/localdevauth/v1/auth/refresh?portalId={portal_id}",
        json={"encodedOAuthRefreshToken": pak},
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("oauthAccessToken", "")


def get_hubspot_metrics(week_start, week_end, prev_start, prev_end, existing):
    token = get_hubspot_token()
    if not token:
        print("  HUBSPOT_PAK / HUBSPOT_TOKEN not set — preserving existing HubSpot values.")
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

    def to_ms(d, end_of_day=False):
        dt = datetime(d.year, d.month, d.day,
                      23 if end_of_day else 0,
                      59 if end_of_day else 0,
                      59 if end_of_day else 0,
                      tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)

    def date_filters(prop, start, end):
        return [{"filters": [
            {"propertyName": prop, "operator": "GTE", "value": str(to_ms(start))},
            {"propertyName": prop, "operator": "LTE", "value": str(to_ms(end, end_of_day=True))},
        ]}]

    print("  Fetching HubSpot metrics...")
    apps_a  = hs_count(date_filters("step_1_completed_date", week_start, week_end))
    apps_b  = hs_count(date_filters("step_1_completed_date", prev_start, prev_end))
    step2_a = hs_count(date_filters("step_2_completed_date", week_start, week_end))
    step2_b = hs_count(date_filters("step_2_completed_date", prev_start, prev_end))

    icp_filters_a = [{"filters": [
        {"propertyName": "step_1_completed_date", "operator": "GTE", "value": str(to_ms(week_start))},
        {"propertyName": "step_1_completed_date", "operator": "LTE", "value": str(to_ms(week_end, end_of_day=True))},
        {"propertyName": "pavilion_icp",          "operator": "EQ",  "value": "true"},
    ]}]
    icp_filters_b = [{"filters": [
        {"propertyName": "step_1_completed_date", "operator": "GTE", "value": str(to_ms(prev_start))},
        {"propertyName": "step_1_completed_date", "operator": "LTE", "value": str(to_ms(prev_end, end_of_day=True))},
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

    existing = {}
    try:
        with open(OUTPUT_FILE) as f:
            existing = json.load(f)
    except Exception:
        pass

    print("Fetching GA4 metrics...")
    ga4_token = get_ga4_token()
    sessions_a, sessions_b, new_users_a, new_users_b = get_ga4_health(
        ga4_token, week_start, week_end, prev_start, prev_end)

    if sessions_a == 0:
        print("No GA4 sessions found for this week — data may not be ready yet. Keeping existing data.json.")
        sys.exit(0)

    traffic = get_traffic(ga4_token, week_start, week_end, prev_start, prev_end)

    print("Fetching HubSpot metrics...")
    hs = get_hubspot_metrics(week_start, week_end, prev_start, prev_end, existing)

    apps_a = to_int(str(hs.get('apps', '0')))
    apps_b_prev = to_int(str(existing.get('apps', '0')))
    conv_a = round(apps_a / sessions_a * 100, 1) if sessions_a else 0
    conv_b = round(apps_b_prev / sessions_b * 100, 1) if sessions_b and apps_b_prev else None

    if os.environ.get("HUBSPOT_TOKEN") or os.environ.get("HUBSPOT_PAK"):
        hs['conversion'] = f"{conv_a}%"
        hs['conversion_delta'] = wow_pct(conv_a, conv_b) if conv_b is not None else None

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
