import requests
import os
from datetime import datetime, timedelta
from functools import lru_cache
import time

CALENDLY_BASE = "https://api.calendly.com"
PARIS_OFFSET = 2
WDAY_MAP = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
SLOT_TIMES = [(h, m) for h in range(8, 21) for m in (0, 30)]
FR_MONTHS = ['Jan','Fév','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Déc']

def get_key():
    return os.environ.get("CALENDLY_API_KEY", "")

def headers():
    return {"Authorization": f"Bearer {get_key()}", "Content-Type": "application/json"}

def api_get(url, params=None):
    r = requests.get(url, headers=headers(), params=params, timeout=10)
    r.raise_for_status()
    return r.json()

# ── Cache simple en mémoire (TTL 30 min) ──────────────────────────────────────
_cache = {}

def cache_get(key):
    entry = _cache.get(key)
    if entry and time.time() - entry["ts"] < entry.get("ttl", 1800):
        return entry["data"]
    return None

def cache_set(key, data, ttl=1800):
    _cache[key] = {"data": data, "ts": time.time(), "ttl": ttl}

def cache_clear():
    _cache.clear()

# ── Helpers ───────────────────────────────────────────────────────────────────
def time_to_min(h, m): return h * 60 + m

def overlaps(s1, e1, s2, e2): return s1 < e2 and e1 > s2

def parse_dt_paris(iso_str):
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).replace(tzinfo=None)
    return dt + timedelta(hours=PARIS_OFFSET)

# ── Organisation ──────────────────────────────────────────────────────────────
def get_org_info():
    cached = cache_get("org_info")
    if cached:
        return cached
    me = api_get(f"{CALENDLY_BASE}/users/me")["resource"]
    result = {
        "user_uri": me["uri"],
        "org_uri": me["current_organization"],
    }
    cache_set("org_info", result)
    return result

def get_members():
    cached = cache_get("members")
    if cached:
        return cached
    org = get_org_info()["org_uri"]
    data = api_get(f"{CALENDLY_BASE}/organization_memberships", {"organization": org, "count": 100})
    members = []
    for m in data.get("collection", []):
        u = m.get("user", {})
        name = u.get("name", "?")
        uri  = u.get("uri", "")
        uuid = uri.split("/")[-1]
        email = u.get("email", "")
        members.append({"name": name, "uuid": uuid, "uri": uri, "email": email})
    cache_set("members", members)
    return members

# ── Schedules ─────────────────────────────────────────────────────────────────
def get_schedule(user_uri):
    ckey = f"sched_{user_uri}"
    cached = cache_get(ckey)
    if cached:
        return cached
    data = api_get(f"{CALENDLY_BASE}/user_availability_schedules", {"user": user_uri})
    schedules = data.get("collection", [])
    default = next((s for s in schedules if s.get("default")), schedules[0] if schedules else None)

    working_hours = {i: [] for i in range(7)}
    date_overrides = {}

    if default:
        for rule in default.get("rules", []):
            ivs = rule.get("intervals", [])
            parsed = [(int(iv["from"][:2]), int(iv["from"][3:]), int(iv["to"][:2]), int(iv["to"][3:])) for iv in ivs]
            if rule.get("type") == "wday" and rule.get("wday") in WDAY_MAP:
                working_hours[WDAY_MAP[rule["wday"]]] = parsed
            elif rule.get("type") == "date":
                date_overrides[rule.get("date", "")] = parsed

    result = {"working_hours": working_hours, "date_overrides": date_overrides}
    cache_set(ckey, result)
    return result

# ── Busy times ────────────────────────────────────────────────────────────────
def get_busy(user_uri, start_utc, end_utc):
    data = api_get(f"{CALENDLY_BASE}/user_busy_times", {
        "user": user_uri,
        "start_time": start_utc,
        "end_time": end_utc,
    })
    result = []
    for bt in data.get("collection", []):
        try:
            bs = parse_dt_paris(bt["start_time"])
            be = parse_dt_paris(bt["end_time"])
            result.append((bs, be, bt.get("type", "external")))
        except Exception:
            pass
    return result

# ── Statut d'activité (cache 24h) ─────────────────────────────────────────────
def get_activity_status() -> dict:
    today_key = datetime.utcnow().strftime("%Y-%m-%d")
    ckey = f"activity_{today_key}"
    cached = cache_get(ckey)
    if cached:
        return cached

    members = get_members()
    status = {}
    for m in members:
        uri = m["uri"]
        try:
            sched = get_schedule(uri)
            has_hours = any(len(v) > 0 for v in sched["working_hours"].values())
        except Exception:
            has_hours = False
        try:
            et = api_get(f"{CALENDLY_BASE}/event_types", {"user": uri, "active": "true", "count": 1})
            has_et = len(et.get("collection", [])) > 0
        except Exception:
            has_et = False

        active = has_hours and has_et
        status[m["name"]] = {"active": active, "has_hours": has_hours, "has_event_types": has_et}

    cache_set(ckey, status, ttl=86400)
    return status

# ── Calcul d'une période quelconque ───────────────────────────────────────────
def _build_period_data(start_day: datetime, label: str, cache_key: str) -> dict:
    cached = cache_get(cache_key)
    if cached:
        return cached

    end_day   = start_day + timedelta(days=6)
    start_utc = (start_day - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = (end_day   - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT23:59:59.000000Z")

    week_days   = [start_day + timedelta(days=i) for i in range(7)]
    slot_labels = [f"{h:02d}:{m:02d}" for h, m in SLOT_TIMES]
    day_labels  = [d.strftime("%a %d %b") for d in week_days]

    all_members = get_members()
    activity    = cache_get(f"activity_{datetime.utcnow().strftime('%Y-%m-%d')}") or {}
    members     = [m for m in all_members if activity.get(m["name"], {}).get("active", True)]
    if not members:
        members = all_members  # fallback si activité pas encore calculée
    user_grids = {}

    for member in members:
        user_uri = member["uri"]
        sched    = get_schedule(user_uri)
        busy     = get_busy(user_uri, start_utc, end_utc)
        wh       = sched["working_hours"]
        do       = sched["date_overrides"]

        user_grid = []
        for day_paris in week_days:
            day_str  = day_paris.strftime("%Y-%m-%d")
            wday_idx = day_paris.weekday()
            intervals = do.get(day_str, wh.get(wday_idx, []))
            day_slots = []
            for (sh, sm) in SLOT_TIMES:
                eh = sh + (sm + 30) // 60
                em = (sm + 30) % 60
                slot_s = time_to_min(sh, sm)
                slot_e = time_to_min(eh, em)
                in_working = any(
                    slot_s >= time_to_min(fh, fm) and slot_e <= time_to_min(th, tm)
                    for fh, fm, th, tm in intervals
                )
                if not in_working:
                    day_slots.append("grey")
                    continue
                sdt = day_paris.replace(hour=sh, minute=sm)
                edt = day_paris.replace(hour=eh, minute=em)
                is_booked  = any(bt == "calendly" and overlaps(sdt, edt, bs, be) for bs, be, bt in busy)
                is_blocked = any(bt == "external" and overlaps(sdt, edt, bs, be) for bs, be, bt in busy)
                if is_booked:    day_slots.append("red")
                elif is_blocked: day_slots.append("grey")
                else:            day_slots.append("green")
            user_grid.append(day_slots)

        user_grids[member["name"]] = user_grid

    user_order = [m["name"] for m in members]

    general_grid = []
    for di in range(7):
        day_gen = []
        for si in range(len(SLOT_TIMES)):
            statuses = [user_grids[n][di][si] for n in user_order]
            booked = sum(1 for s in statuses if s == "red")
            free   = sum(1 for s in statuses if s == "green")
            total  = booked + free
            if total == 0:
                day_gen.append({"color": "grey",   "label": "",                 "booked": 0,      "free": 0})
            elif free == 0:
                day_gen.append({"color": "red",    "label": f"{booked}/{total}", "booked": booked, "free": 0})
            elif free == 1:
                day_gen.append({"color": "orange", "label": f"{booked}/{total}", "booked": booked, "free": 1})
            else:
                day_gen.append({"color": "green",  "label": f"{booked}/{total}", "booked": booked, "free": free})
        general_grid.append(day_gen)

    result = {
        "week_label":  label,
        "day_labels":  day_labels,
        "slot_labels": slot_labels,
        "user_order":  user_order,
        "users":       user_grids,
        "general":     general_grid,
    }
    cache_set(cache_key, result)
    return result

# ── Calcul d'une semaine calendaire ───────────────────────────────────────────
BASE_MONDAY = datetime(2026, 5, 11)

def get_week_data(week_offset: int) -> dict:
    monday = BASE_MONDAY + timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)
    label  = f"Semaine du {monday.day} au {sunday.day} {FR_MONTHS[sunday.month - 1]} {sunday.year}"
    return _build_period_data(monday, label, f"week_{week_offset}")

def get_diagnostic() -> list:
    now      = datetime.utcnow()
    past_30  = (now - timedelta(days=30)).strftime("%Y-%m-%dT00:00:00.000000Z")
    future_30 = (now + timedelta(days=30)).strftime("%Y-%m-%dT23:59:59.000000Z")
    now_str  = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    members = get_members()
    results = []

    for m in members:
        uri  = m["uri"]
        name = m["name"]
        info = {"name": name, "email": m["email"]}

        # 1. Horaires configurés
        try:
            sched = get_schedule(uri)
            wh = sched["working_hours"]
            has_hours = any(len(v) > 0 for v in wh.values())
            info["has_hours"] = has_hours
            active_days = sum(1 for v in wh.values() if len(v) > 0)
            info["active_days"] = active_days
        except Exception:
            info["has_hours"] = False
            info["active_days"] = 0

        # 2. Event types actifs
        try:
            et = api_get(f"{CALENDLY_BASE}/event_types", {"user": uri, "active": "true", "count": 10})
            event_types = [e["name"] for e in et.get("collection", [])]
            info["event_types"] = event_types
            info["has_event_types"] = len(event_types) > 0
        except Exception:
            info["event_types"] = []
            info["has_event_types"] = False

        # 3. RDV posés ces 30 derniers jours
        try:
            past = api_get(f"{CALENDLY_BASE}/scheduled_events", {
                "user": uri, "min_start_time": past_30, "max_start_time": now_str,
                "status": "active", "count": 100
            })
            info["rdv_past_30"] = len(past.get("collection", []))
        except Exception:
            info["rdv_past_30"] = None

        # 4. RDV à venir (30 prochains jours)
        try:
            fut = api_get(f"{CALENDLY_BASE}/scheduled_events", {
                "user": uri, "min_start_time": now_str, "max_start_time": future_30,
                "status": "active", "count": 100
            })
            info["rdv_next_30"] = len(fut.get("collection", []))
        except Exception:
            info["rdv_next_30"] = None

        # Score d'activité
        score = 0
        if info["has_hours"]:       score += 1
        if info["has_event_types"]: score += 1
        if (info["rdv_past_30"] or 0) > 0:  score += 1
        if (info["rdv_next_30"] or 0) > 0:  score += 1
        info["score"] = score  # 0=inactif, 4=pleinement actif

        results.append(info)

    results.sort(key=lambda x: -x["score"])
    return results

def get_next7_data() -> dict:
    today = (datetime.utcnow() + timedelta(hours=PARIS_OFFSET)).replace(hour=0, minute=0, second=0, microsecond=0)
    end   = today + timedelta(days=6)
    label = f"7 prochains jours — {today.day} {FR_MONTHS[today.month-1]} au {end.day} {FR_MONTHS[end.month-1]}"
    return _build_period_data(today, label, f"next7_{today.strftime('%Y-%m-%d')}")


# ── Événements planifiés ──────────────────────────────────────────────────────
def get_scheduled_events(user_uri, start_utc, end_utc):
    try:
        data = api_get(f"{CALENDLY_BASE}/scheduled_events", {
            "user": user_uri,
            "min_start_time": start_utc,
            "max_start_time": end_utc,
            "status": "active",
            "count": 100,
        })
    except Exception:
        return []
    events = []
    for e in data.get("collection", []):
        try:
            s = parse_dt_paris(e["start_time"])
            f = parse_dt_paris(e["end_time"])
            events.append({
                "name":     e.get("name", "RDV"),
                "start":    s.strftime("%H:%M"),
                "end":      f.strftime("%H:%M"),
                "date":     s.strftime("%Y-%m-%d"),
                "duration": int((f - s).total_seconds() / 60),
            })
        except Exception:
            pass
    return sorted(events, key=lambda x: x["start"])


def get_events_week_data(week_offset: int) -> dict:
    ckey = f"events_{week_offset}"
    cached = cache_get(ckey)
    if cached:
        return cached

    monday = BASE_MONDAY + timedelta(weeks=week_offset)
    sunday = monday + timedelta(days=6)
    start_utc = monday.strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = sunday.strftime("%Y-%m-%dT23:59:59.000000Z")

    all_members  = get_members()
    activity     = cache_get(f"activity_{datetime.utcnow().strftime('%Y-%m-%d')}") or {}
    members      = [m for m in all_members if activity.get(m["name"], {}).get("active", True)]
    if not members:
        members = all_members

    events_by_member = {}
    for m in members:
        events_by_member[m["name"]] = get_scheduled_events(m["uri"], start_utc, end_utc)

    week_days = [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    day_labels = []
    FR_DAYS_LONG = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    for i in range(7):
        d = monday + timedelta(days=i)
        day_labels.append(f"{FR_DAYS_LONG[i]} {d.day} {FR_MONTHS[d.month-1]}")

    result = {
        "week_label":       f"Semaine du {monday.day} au {sunday.day} {FR_MONTHS[sunday.month-1]} {sunday.year}",
        "week_days":        week_days,
        "day_labels":       day_labels,
        "events_by_member": events_by_member,
        "member_names":     [m["name"] for m in members],
    }
    cache_set(ckey, result, ttl=1800)
    return result


def get_events_next7_data() -> dict:
    today  = (datetime.utcnow() + timedelta(hours=PARIS_OFFSET)).replace(hour=0, minute=0, second=0, microsecond=0)
    end    = today + timedelta(days=6)
    ckey   = f"events_next7_{today.strftime('%Y-%m-%d')}"
    cached = cache_get(ckey)
    if cached:
        return cached

    start_utc = today.strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = end.strftime("%Y-%m-%dT23:59:59.000000Z")

    all_members = get_members()
    activity    = cache_get(f"activity_{today.strftime('%Y-%m-%d')}") or {}
    members     = [m for m in all_members if activity.get(m["name"], {}).get("active", True)]
    if not members:
        members = all_members

    events_by_member = {}
    for m in members:
        events_by_member[m["name"]] = get_scheduled_events(m["uri"], start_utc, end_utc)

    FR_DAYS_LONG = ["Lundi","Mardi","Mercredi","Jeudi","Vendredi","Samedi","Dimanche"]
    week_days  = [(today + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]
    day_labels = []
    for i in range(7):
        d = today + timedelta(days=i)
        wday = d.weekday()
        day_labels.append(f"{FR_DAYS_LONG[wday]} {d.day} {FR_MONTHS[d.month-1]}")

    result = {
        "week_label":       f"7 prochains jours — {today.day} {FR_MONTHS[today.month-1]} au {end.day} {FR_MONTHS[end.month-1]}",
        "week_days":        week_days,
        "day_labels":       day_labels,
        "events_by_member": events_by_member,
        "member_names":     [m["name"] for m in members],
    }
    cache_set(ckey, result, ttl=1800)
    return result
