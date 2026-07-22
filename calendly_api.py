import requests
import os
from datetime import datetime, timedelta
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo
import time

CALENDLY_BASE = "https://api.calendly.com"
PARIS_OFFSET = 2
WDAY_MAP = {"monday":0,"tuesday":1,"wednesday":2,"thursday":3,"friday":4,"saturday":5,"sunday":6}
SLOT_TIMES       = [(h, m) for h in range(8, 21) for m in (0, 30)]
AVAIL_SLOT_TIMES = [(h, m) for h in range(0, 24) for m in (0, 30)]
FR_MONTHS = ['Jan','Fév','Mar','Avr','Mai','Jun','Jul','Aoû','Sep','Oct','Nov','Déc']

def get_key():
    return os.environ.get("CALENDLY_API_KEY", "")

def headers():
    return {"Authorization": f"Bearer {get_key()}", "Content-Type": "application/json"}

def api_get(url, params=None, _retry=3):
    """GET Calendly avec retry automatique sur 429 (rate limit)."""
    for attempt in range(_retry):
        r = requests.get(url, headers=headers(), params=params, timeout=10)
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 5)) + 1
            time.sleep(wait)
            continue
        r.raise_for_status()
        return r.json()
    raise Exception(f"Rate limit persistant après {_retry} tentatives : {url}")

def api_get_all_pages(url, params=None):
    """Récupère toutes les pages d'un endpoint paginé Calendly."""
    all_items = []
    p = dict(params or {})
    p.setdefault("count", 100)
    current_url = url
    current_params = p
    while True:
        data = api_get(current_url, current_params)
        all_items.extend(data.get("collection", []))
        next_page = data.get("pagination", {}).get("next_page")
        if not next_page:
            break
        current_url   = next_page
        current_params = {}   # next_page inclut déjà tous les params
    return all_items

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
_PARIS_TZ = ZoneInfo("Europe/Paris")
_REF_MONDAY = datetime(2026, 7, 20)  # lundi de référence pour la conversion (LUN 20 jul 2026)

def _to_paris_intervals(intervals_by_wday, source_tz_str):
    """Convertit les intervalles de travail du fuseau Calendly vers Europe/Paris."""
    try:
        src_tz = ZoneInfo(source_tz_str)
        # Même offset que Paris ? (vérifié en été)
        ref = datetime(2026, 7, 15, 12, 0, tzinfo=src_tz)
        if ref.utcoffset() == datetime(2026, 7, 15, 12, 0, tzinfo=_PARIS_TZ).utcoffset():
            return intervals_by_wday
    except Exception:
        return intervals_by_wday

    new_wh = {i: [] for i in range(7)}
    for wday, intervals in intervals_by_wday.items():
        ref_date = _REF_MONDAY + timedelta(days=wday)
        for (fh, fm, th, tm) in intervals:
            dt_f = datetime(ref_date.year, ref_date.month, ref_date.day, fh, fm, tzinfo=src_tz)
            # "to" == 00:00 → minuit = début du jour suivant
            if th == 0 and tm == 0:
                dt_t = datetime(ref_date.year, ref_date.month, ref_date.day, tzinfo=src_tz) + timedelta(days=1)
            else:
                dt_t = datetime(ref_date.year, ref_date.month, ref_date.day, th, tm, tzinfo=src_tz)
            pf = dt_f.astimezone(_PARIS_TZ)
            pt = dt_t.astimezone(_PARIS_TZ)
            pf_wd, pt_wd = pf.weekday(), pt.weekday()
            if pf_wd == pt_wd:
                new_wh[pf_wd].append((pf.hour, pf.minute, pt.hour, pt.minute))
            else:
                # Chevauchement minuit : 24:00 = 1440 min, géré par time_to_min
                new_wh[pf_wd].append((pf.hour, pf.minute, 24, 0))
                if pt.hour > 0 or pt.minute > 0:
                    new_wh[pt_wd % 7].append((0, 0, pt.hour, pt.minute))
    return new_wh


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
    source_tz = "Europe/Paris"

    if default:
        source_tz = default.get("timezone", "Europe/Paris")
        for rule in default.get("rules", []):
            ivs = rule.get("intervals", [])
            parsed = [(int(iv["from"][:2]), int(iv["from"][3:]), int(iv["to"][:2]), int(iv["to"][3:])) for iv in ivs]
            if rule.get("type") == "wday" and rule.get("wday") in WDAY_MAP:
                working_hours[WDAY_MAP[rule["wday"]]] = parsed
            elif rule.get("type") == "date":
                date_overrides[rule.get("date", "")] = parsed

    working_hours = _to_paris_intervals(working_hours, source_tz)

    result = {"working_hours": working_hours, "date_overrides": date_overrides}
    cache_set(ckey, result, ttl=86400)
    return result

# ── Busy times ────────────────────────────────────────────────────────────────
def get_busy(user_uri, start_utc, end_utc):
    ckey = f"busy_{user_uri}_{start_utc[:10]}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
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
    cache_set(ckey, result, ttl=120)  # busy times : cache 2 min (annulations prises en compte rapidement)
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

        active = has_hours or has_et   # actif si horaires OU event types configurés
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
    members     = all_members  # Tous les membres, actifs ou non (l'activité gère seulement le dot couleur en UI)
    user_grids  = {}

    for member in members:
        user_uri = member["uri"]
        try:
            sched = get_schedule(user_uri)
            busy  = get_busy(user_uri, start_utc, end_utc)
            wh    = sched["working_hours"]
            do    = sched["date_overrides"]
        except Exception as ex:
            print(f"[Calendar] skip {member['name']}: {ex}")
            # Membre sans horaires : tous les slots gris (pas de dispo)
            user_grids[member["name"]] = [["grey"] * len(SLOT_TIMES) for _ in week_days]
            continue

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
                # Rouge uniquement si l'événement DÉMARRE dans ce slot (≠ continuation)
                # → 1 case rouge = 1 RDV, quelle que soit la durée
                is_booked  = any(bt == "calendly" and sdt <= bs < edt for bs, be, bt in busy)
                is_blocked = any(bt == "external"  and overlaps(sdt, edt, bs, be) for bs, be, bt in busy)
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
        "week_days":   [d.strftime("%Y-%m-%d") for d in week_days],
        "day_labels":  day_labels,
        "slot_labels": slot_labels,
        "user_order":  user_order,
        "users":       user_grids,
        "general":     general_grid,
    }
    cache_set(cache_key, result)
    return result

# ── Event types d'un closer (avec durée + buffer) ─────────────────────────────
def get_user_event_types(user_uri: str) -> list:
    """Retourne les event types actifs d'un closer (uri + durée). Cache 1h."""
    ckey = f"etypes_{user_uri}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    try:
        data = api_get(f"{CALENDLY_BASE}/event_types", {"user": user_uri, "active": "true", "count": 10})
        result = [
            {"uri": e["uri"], "name": e.get("name", ""), "duration": e.get("duration", 30)}
            for e in data.get("collection", []) if e.get("uri")
        ]
    except Exception:
        result = []
    cache_set(ckey, result, ttl=3600)
    return result


# ── Events org-level pour la semaine (pour disponibilité + invités) ────────────
def _get_org_events_week(start_utc: str, end_utc: str) -> dict:
    """Fetch all active org events for the week, grouped by user URI. Cache 5 min."""
    ckey = f"org_events_{start_utc[:10]}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    org_uri = get_org_info()["org_uri"]
    raw = api_get_all_pages(
        f"{CALENDLY_BASE}/scheduled_events",
        {"organization": org_uri, "status": "active",
         "min_start_time": start_utc, "max_start_time": end_utc}
    )
    by_user = {}
    for e in raw:
        memberships = e.get("event_memberships", [])
        if not memberships:
            continue
        user_uri = memberships[0].get("user", "")
        s = parse_dt_paris(e["start_time"])
        f = parse_dt_paris(e["end_time"])
        by_user.setdefault(user_uri, []).append({
            "name":     e.get("name", "RDV"),
            "start":    s.strftime("%H:%M"),
            "end":      f.strftime("%H:%M"),
            "date":     s.strftime("%Y-%m-%d"),
            "duration": int((f - s).total_seconds() / 60),
            "uri":      e.get("uri", ""),
        })
    cache_set(ckey, by_user, ttl=90)  # 90s — les annulations doivent disparaître vite
    return by_user


# ── Disponibilité des closers (5 états) ───────────────────────────────────────
def build_availability_week(start_day: datetime) -> dict:
    """
    Retourne la disponibilité de chaque closer pour la semaine commençant start_day.
    États par slot de 30 min :
      - "unavailable"  : en dehors des heures paramétrées Calendly  → gris foncé
      - "available"    : libre, pas de blocage
      - "blocked"      : bloqué par l'agenda externe (Google Cal)    → rouge
      - "buffer"       : indispo Calendly avant/après un RDV         → orange clair
      - {type:"booked"}: RDV Calendly avec détails invité            → vert
    """
    ckey = f"avail_{start_day.strftime('%Y-%m-%d')}"
    cached = cache_get(ckey)
    if cached:
        return cached

    end_day   = start_day + timedelta(days=6)
    start_utc = (start_day - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = (end_day   - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT23:59:59.000000Z")
    week_days = [start_day + timedelta(days=i) for i in range(7)]

    members    = get_members()
    user_order = [m["name"] for m in members]
    users_data = {}

    # Une seule requête org-level pour tous les events de la semaine
    try:
        all_events_by_user = _get_org_events_week(start_utc, end_utc)
    except Exception:
        all_events_by_user = {}

    def _fetch_member(member):
        user_uri = member["uri"]
        name     = member["name"]
        try:
            sched = get_schedule(user_uri)
            busy  = get_busy(user_uri, start_utc, end_utc)
            wh    = sched["working_hours"]
            do    = sched["date_overrides"]
        except Exception as ex:
            print(f"[Availability] skip {name}: {ex}")
            return name, {"days": [{"slots": ["unavailable"] * len(AVAIL_SLOT_TIMES)} for _ in week_days]}

        # Events de ce closer + invités (parallèle, cache 30 min)
        raw_events = all_events_by_user.get(user_uri, [])
        def _add_invitee(ev):
            invs = get_event_invitees(ev["uri"]) if ev.get("uri") else []
            return {**ev, "invitee": invs[0]["name"] if invs else None}
        events = []
        if raw_events:
            with ThreadPoolExecutor(max_workers=min(len(raw_events), 6)) as pool:
                events = list(pool.map(_add_invitee, raw_events))

        days = []
        for day_paris in week_days:
            day_str    = day_paris.strftime("%Y-%m-%d")
            wday_idx   = day_paris.weekday()
            intervals  = do.get(day_str, wh.get(wday_idx, []))
            day_events = [e for e in events if e["date"] == day_str]
            slots      = []
            for (sh, sm) in AVAIL_SLOT_TIMES:
                eh = sh + (sm + 30) // 60
                em = (sm + 30) % 60
                slot_s = time_to_min(sh, sm)
                slot_e = time_to_min(eh, em)
                in_working = any(
                    slot_s >= time_to_min(fh, fm) and slot_e <= time_to_min(th, tm)
                    for fh, fm, th, tm in intervals
                )
                if not in_working:
                    slots.append("unavailable")
                    continue
                sdt = day_paris.replace(hour=sh, minute=sm)
                edt = day_paris.replace(hour=eh, minute=em)

                # Cherche un RDV Calendly qui chevauche ce slot
                matched = None
                for ev in day_events:
                    ev_s = time_to_min(int(ev["start"][:2]), int(ev["start"][3:]))
                    ev_e = time_to_min(int(ev["end"][:2]),   int(ev["end"][3:]))
                    if ev_s < slot_e and ev_e > slot_s:
                        matched = ev
                        if ev_s == slot_s:
                            break

                if matched:
                    is_start = (time_to_min(int(matched["start"][:2]), int(matched["start"][3:])) == slot_s)
                    slots.append({
                        "type":       "booked",
                        "event_name": matched["name"],
                        "invitee":    matched.get("invitee"),
                        "start":      matched["start"],
                        "end":        matched["end"],
                        "duration":   matched["duration"],
                        "is_start":   is_start,
                    })
                elif any(bt == "calendly" and overlaps(sdt, edt, bs, be) for bs, be, bt in busy):
                    # Calendly busy sans RDV détecté = buffer avant/après call
                    slots.append("buffer")
                elif any(bt == "external" and overlaps(sdt, edt, bs, be) for bs, be, bt in busy):
                    slots.append("blocked")
                else:
                    slots.append("available")
            days.append({"date": day_str, "slots": slots})

        # Compte les vrais créneaux bookables via Calendly natif
        # (prend en compte durée du RDV + buffer avant/après + agenda externe)
        dispo_count = 0
        try:
            ets = get_user_event_types(user_uri)
            # Event type principal = 60 min en priorité, sinon le premier actif
            primary = next((e for e in ets if e["duration"] == 60), ets[0] if ets else None)
            if primary:
                avail = get_event_type_available_times([primary["uri"]], start_utc, end_utc)
                dispo_count = sum(len(times) for times in avail.values())
        except Exception:
            pass

        return name, {"days": days, "dispo_count": dispo_count}

    with ThreadPoolExecutor(max_workers=len(members)) as ex:
        for name, data in ex.map(_fetch_member, members):
            users_data[name] = data

    slot_labels = [f"{h:02d}:{m:02d}" for h, m in AVAIL_SLOT_TIMES]
    day_labels  = [d.strftime("%Y-%m-%d") for d in week_days]
    result = {
        "start_date":  start_day.strftime("%Y-%m-%d"),
        "end_date":    end_day.strftime("%Y-%m-%d"),
        "week_label":  f"Semaine du {start_day.day} au {end_day.day} {FR_MONTHS[end_day.month-1]} {end_day.year}",
        "slot_labels": slot_labels,
        "day_dates":   day_labels,
        "user_order":  user_order,
        "users":       users_data,
    }
    cache_set(ckey, result, ttl=120)  # 2 min — annulations reflétées rapidement
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


# ── Noms de tous les event types configurés (pas juste ceux avec des RDV) ────
def get_all_event_type_names() -> list:
    ckey = "event_type_names"
    cached = cache_get(ckey)
    if cached:
        return cached

    all_members = get_members()
    activity    = cache_get(f"activity_{datetime.utcnow().strftime('%Y-%m-%d')}") or {}
    members     = [m for m in all_members if activity.get(m["name"], {}).get("active", True)]
    if not members:
        members = all_members

    types        = {}  # name → {url, uris: [per-member URI, ...]}
    members_by_type = {}  # name → [member_name, ...]
    for m in members:
        try:
            et = api_get(f"{CALENDLY_BASE}/event_types", {"user": m["uri"], "active": "true", "count": 100})
            for e in et.get("collection", []):
                n = e.get("name", "").strip()
                if not n:
                    continue
                if n not in types:
                    types[n] = {
                        "url":      e.get("scheduling_url", ""),
                        "uris":     [],
                        "duration": e.get("duration", 30),  # durée en minutes
                    }
                    members_by_type[n] = []
                uri = e.get("uri", "")
                if uri and uri not in types[n]["uris"]:
                    types[n]["uris"].append(uri)
                if m["name"] not in members_by_type[n]:
                    members_by_type[n].append(m["name"])
        except Exception:
            pass

    result = [{"name": n, "url": types[n]["url"], "uris": types[n]["uris"],
               "duration": types[n]["duration"], "members": members_by_type.get(n, [])} for n in sorted(types)]
    cache_set(ckey, result, ttl=3600)   # 1h — les event types changent rarement
    return result

# ── Créneaux réels d'un event type (logique Calendly native) ─────────────────
def _fetch_available_times_for_uri(uri: str, start_utc: str, end_utc: str) -> dict:
    """Récupère les créneaux disponibles pour UN uri d'event type spécifique."""
    try:
        data = api_get(f"{CALENDLY_BASE}/event_type_available_times", {
            "event_type": uri,
            "start_time": start_utc,
            "end_time":   end_utc,
        })
        slots_by_date = {}
        for item in data.get("collection", []):
            if item.get("status") != "available":
                continue
            st = item.get("start_time", "")
            if not st:
                continue
            dt_paris = parse_dt_paris(st)
            date_str = dt_paris.strftime("%Y-%m-%d")
            time_str = dt_paris.strftime("%H:%M")
            slots_by_date.setdefault(date_str, set())
            slots_by_date[date_str].add(time_str)
        return {d: sorted(list(s)) for d, s in slots_by_date.items()}
    except Exception as ex:
        print(f"[available_times] {uri.split('/')[-1]}: {ex}")
        return {}

def get_event_type_available_times(event_type_uris, start_utc: str, end_utc: str) -> dict:
    """
    Retourne les vrais créneaux disponibles pour un event type via l'API Calendly.
    Accepte un URI string OU une liste d'URIs (round-robin → union des dispos).
    Prend en compte : durée, buffer, délai minimum, limite de RDV/jour.
    Retourne : {date: [slots_HH:MM]}
    """
    if isinstance(event_type_uris, str):
        event_type_uris = [event_type_uris]

    uris_key = "_".join(u.split("/")[-1] for u in event_type_uris[:3])
    ckey = f"avail_{uris_key}_{start_utc[:10]}_{end_utc[:10]}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached

    merged = {}
    for uri in event_type_uris:
        for date_str, times in _fetch_available_times_for_uri(uri, start_utc, end_utc).items():
            if date_str not in merged:
                merged[date_str] = set()
            merged[date_str].update(times)

    result = {d: sorted(list(s)) for d, s in merged.items()}
    cache_set(ckey, result, ttl=900)  # 15 min — la dispo change souvent
    return result

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
                "uri":      e.get("uri", ""),
            })
        except Exception:
            pass
    return sorted(events, key=lambda x: x["start"])


def get_event_invitees(event_uri: str) -> list:
    """Retourne les noms des participants (leads) pour un événement donné."""
    if not event_uri:
        return []
    uuid = event_uri.rstrip("/").split("/")[-1]
    ckey = f"invitees_{uuid}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached
    try:
        data = api_get(f"{CALENDLY_BASE}/scheduled_events/{uuid}/invitees", {"count": 10})
        invitees = [
            {
                "name":            inv.get("name", ""),
                "email":           inv.get("email", ""),
                "cancel_url":      inv.get("cancel_url", ""),
                "reschedule_url":  inv.get("reschedule_url", ""),
            }
            for inv in data.get("collection", [])
        ]
    except Exception:
        invitees = []
    # Ne pas cacher les listes vides — un appel raté (rate limit, erreur) ne doit pas polluer le cache
    if invitees:
        cache_set(ckey, invitees, ttl=1800)
    return invitees


def get_slot_invitees(member_name: str, date: str, time: str) -> dict:
    """Retourne l'événement + les invités pour un membre/date/créneau donnés."""
    ckey = f"slot_inv_{member_name}_{date}_{time.replace(':', '')}"
    cached = cache_get(ckey)
    if cached is not None:
        return cached

    # Trouver l'URI du membre
    members = get_members()
    member  = next((m for m in members if m["name"] == member_name), None)
    if not member:
        result = {"invitees": [], "event": None, "error": "membre introuvable"}
        cache_set(ckey, result, ttl=300)
        return result

    # Plage UTC couvrant toute la journée Paris
    day       = datetime.strptime(date, "%Y-%m-%d")
    start_utc = (day - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = (day - timedelta(hours=PARIS_OFFSET) + timedelta(days=1)).strftime("%Y-%m-%dT00:00:00.000000Z")

    # Requête org-level pour couvrir tous les membres (y compris non-propriétaire du token)
    try:
        org_uri = get_org_info()["org_uri"]
        raw = api_get_all_pages(
            f"{CALENDLY_BASE}/scheduled_events",
            {"organization": org_uri, "status": "active",
             "min_start_time": start_utc, "max_start_time": end_utc}
        )
        events = []
        for e in raw:
            memberships = e.get("event_memberships", [])
            if not memberships:
                continue
            # Filtrer sur ce membre uniquement
            if memberships[0].get("user", "") != member["uri"]:
                continue
            s = parse_dt_paris(e["start_time"])
            f = parse_dt_paris(e["end_time"])
            events.append({
                "name":     e.get("name", "RDV"),
                "start":    s.strftime("%H:%M"),
                "end":      f.strftime("%H:%M"),
                "date":     s.strftime("%Y-%m-%d"),
                "duration": int((f - s).total_seconds() / 60),
                "uri":      e.get("uri", ""),
            })
    except Exception:
        events = get_scheduled_events(member["uri"], start_utc, end_utc)

    # Trouver l'événement qui chevauche le créneau de 30 min
    slotH, slotM = (int(x) for x in time.split(':'))
    slot_s = slotH * 60 + slotM
    slot_e = slot_s + 30

    matched = None
    for e in events:
        eH, eM = (int(x) for x in e["start"].split(':'))
        fH, fM = (int(x) for x in e["end"].split(':'))
        if eH * 60 + eM < slot_e and fH * 60 + fM > slot_s:
            matched = e
            # Priorité à l'événement qui commence exactement sur le créneau
            if e["start"] == time:
                break

    if not matched or not matched.get("uri"):
        result = {"invitees": [], "event": matched}
        cache_set(ckey, result, ttl=300)
        return result

    invitees = get_event_invitees(matched["uri"])
    result   = {"invitees": invitees, "event": matched}
    cache_set(ckey, result, ttl=1800)
    return result


def _build_events_by_member_org(start_utc, end_utc, members):
    """
    Requête organisation pour récupérer les événements de TOUS les membres,
    y compris les membres désactivés dont on veut garder l'historique.
    """
    all_members = get_members()
    uri_to_name = {m["uri"]: m["name"] for m in all_members}
    # Noms des membres actifs passés en paramètre (pour initialiser le dict)
    active_names = {m["name"] for m in members}

    events_by_member = {m["name"]: [] for m in members}
    try:
        org_uri = get_org_info()["org_uri"]
        all_events = api_get_all_pages(
            f"{CALENDLY_BASE}/scheduled_events",
            {"organization": org_uri, "status": "active",
             "min_start_time": start_utc, "max_start_time": end_utc}
        )
        for e in all_events:
            memberships = e.get("event_memberships", [])
            if not memberships:
                continue
            user_uri    = memberships[0].get("user", "")
            # Nom depuis le mapping actif, sinon depuis le champ user_name de l'event
            member_name = uri_to_name.get(user_uri, memberships[0].get("user_name", ""))
            if not member_name:
                continue
            # Inclure même les membres désactivés (historique)
            if member_name not in events_by_member:
                events_by_member[member_name] = []
            try:
                s = parse_dt_paris(e["start_time"])
                f = parse_dt_paris(e["end_time"])
                events_by_member[member_name].append({
                    "name":     e.get("name", "RDV"),
                    "start":    s.strftime("%H:%M"),
                    "end":      f.strftime("%H:%M"),
                    "date":     s.strftime("%Y-%m-%d"),
                    "duration": int((f - s).total_seconds() / 60),
                    "uri":      e.get("uri", ""),
                })
            except Exception:
                pass
    except Exception as ex:
        print(f"[events_org] error: {ex}")

    for name in events_by_member:
        events_by_member[name].sort(key=lambda x: (x["date"], x["start"]))
    return events_by_member


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
    members      = all_members  # Tous les membres, actifs ou non

    events_by_member = _build_events_by_member_org(start_utc, end_utc, members)

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
    members     = all_members  # Tous les membres, actifs ou non

    events_by_member = _build_events_by_member_org(start_utc, end_utc, members)

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


# ── Import historique vers Supabase ───────────────────────────────────────────
def get_all_bookings_for_import(days_past: int = 90, days_future: int = 30) -> list:
    """
    Retourne tous les RDV (passés + futurs) avec invités pour toute l'organisation.
    Utilise le paramètre 'organization' (au lieu de 'user') pour récupérer tous les
    membres même si le token n'a accès direct qu'à son propre compte.
    """
    now       = datetime.utcnow()
    start_utc = (now - timedelta(days=days_past)).strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = (now + timedelta(days=days_future)).strftime("%Y-%m-%dT23:59:59.000000Z")

    org_uri     = get_org_info()["org_uri"]
    all_members = get_members()
    # Construire un dict URI → name pour résoudre le membre depuis event_memberships
    uri_to_name = {m["uri"]: m["name"] for m in all_members}

    raw_events = []
    for fetch_status in ("active", "canceled"):
        try:
            events = api_get_all_pages(
                f"{CALENDLY_BASE}/scheduled_events",
                {"organization": org_uri, "status": fetch_status,
                 "min_start_time": start_utc, "max_start_time": end_utc}
            )
            for e in events:
                memberships = e.get("event_memberships", [])
                member_name = ""
                if memberships:
                    user_uri    = memberships[0].get("user", "")
                    member_name = uri_to_name.get(user_uri, memberships[0].get("user_name", ""))
                raw_events.append((member_name, e, fetch_status))
        except Exception as ex:
            print(f"[import] org query error ({fetch_status}): {ex}")

    def build_booking(member_name_event_status):
        member_name, e, evt_status = member_name_event_status
        try:
            s = parse_dt_paris(e["start_time"])
            booking = {
                "event_uri":   e.get("uri", ""),
                "member_name": member_name,
                "event_type":  e.get("name", ""),
                "start_time":  s.strftime("%H:%M"),
                "date":        s.strftime("%Y-%m-%d"),
                "status":      evt_status,
                "lead_name":   "",
                "lead_email":  "",
                "created_at":  e.get("created_at", ""),
            }
            invitees = get_event_invitees(e.get("uri", ""))
            if invitees:
                booking["lead_name"]  = invitees[0].get("name", "")
                booking["lead_email"] = invitees[0].get("email", "")
            return booking
        except Exception as ex:
            print(f"[import] build error: {ex}")
            return None

    # 3 workers max pour éviter le rate limit Calendly sur les invitees (~100 req/min)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(build_booking, raw_events))

    return [b for b in results if b is not None]


# ── Disponibilité v2 : bookings depuis Supabase + schedule/busy Calendly ───────
def build_availability_week_v2(start_day: datetime, bookings_by_member: dict) -> dict:
    """
    Calcule la disponibilité en utilisant les bookings pré-fetchés depuis Supabase.
    Appels Calendly restants : get_schedule (cache 24h) + get_busy (cache 2 min).
    États par slot 30 min :
      - "unavailable"  : hors plage Calendly              → gris foncé
      - "blocked"      : bloqué agenda externe (Google)   → rouge
      - "buffer"       : indispo Calendly avant/après RDV → orange
      - {type:"booked"}: RDV posé (détails depuis Supabase) → vert
      - "available"    : libre
    """
    ckey = f"avail2_{start_day.strftime('%Y-%m-%d')}"
    cached = cache_get(ckey)
    if cached:
        return cached

    end_day   = start_day + timedelta(days=6)
    start_utc = (start_day - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT00:00:00.000000Z")
    end_utc   = (end_day   - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT23:59:59.000000Z")
    week_days = [start_day + timedelta(days=i) for i in range(7)]

    members    = get_members()
    user_order = [m["name"] for m in members]
    users_data = {}

    def _fetch_member(member):
        user_uri = member["uri"]
        name     = member["name"]
        try:
            sched = get_schedule(user_uri)
            busy  = get_busy(user_uri, start_utc, end_utc)
            wh    = sched["working_hours"]
            do    = sched["date_overrides"]
        except Exception as ex:
            print(f"[Avail v2] skip {name}: {ex}")
            return name, {"days": [{"date": d.strftime("%Y-%m-%d"), "slots": ["unavailable"] * len(AVAIL_SLOT_TIMES)} for d in week_days], "dispo_count": 0}

        # Bookings Supabase → map par jour (durée par défaut 60 min si end_time absent)
        day_events_map: dict = {}
        for b in bookings_by_member.get(name, []):
            d  = b.get("date", "")
            sh, sm = (int(x) for x in b.get("start_time", "00:00").split(":"))
            total_end = sh * 60 + sm + 60
            eh, em    = total_end // 60, total_end % 60
            day_events_map.setdefault(d, []).append({
                "event_name": b.get("event_type", "RDV"),
                "invitee":    b.get("lead_name") or None,
                "start":      b["start_time"],
                "end":        f"{eh:02d}:{em:02d}",
                "duration":   60,
            })

        days        = []
        dispo_count = 0

        for day_paris in week_days:
            day_str    = day_paris.strftime("%Y-%m-%d")
            wday_idx   = day_paris.weekday()
            intervals  = do.get(day_str, wh.get(wday_idx, []))
            ev_today   = day_events_map.get(day_str, [])
            slots      = []

            for (sh, sm) in AVAIL_SLOT_TIMES:
                eh     = sh + (sm + 30) // 60
                em     = (sm + 30) % 60
                slot_s = time_to_min(sh, sm)
                slot_e = time_to_min(eh, em)
                in_working = any(
                    slot_s >= time_to_min(fh, fm) and slot_e <= time_to_min(th, tm)
                    for fh, fm, th, tm in intervals
                )
                sdt = day_paris.replace(hour=sh, minute=sm)
                edt = day_paris.replace(hour=eh, minute=em)

                # 1. Booking Supabase → "booked" même hors plage Calendly
                matched = None
                for ev in ev_today:
                    ev_s = time_to_min(int(ev["start"][:2]), int(ev["start"][3:]))
                    ev_e = time_to_min(int(ev["end"][:2]),   int(ev["end"][3:]))
                    if ev_s < slot_e and ev_e > slot_s:
                        matched = ev
                        if ev_s == slot_s:
                            break

                if not in_working and not matched:
                    slots.append("unavailable")
                    continue

                if matched:
                    is_start = (time_to_min(int(matched["start"][:2]), int(matched["start"][3:])) == slot_s)
                    slots.append({"type": "booked", "event_name": matched["event_name"],
                                  "invitee": matched["invitee"], "start": matched["start"],
                                  "end": matched["end"], "duration": matched["duration"],
                                  "is_start": is_start})
                # 2. Busy Calendly sans booking Supabase → buffer (indispo avant/après)
                elif any(bt == "calendly" and overlaps(sdt, edt, bs, be) for bs, be, bt in busy):
                    slots.append("buffer")
                # 3. Agenda externe → bloqué
                elif any(bt == "external" and overlaps(sdt, edt, bs, be) for bs, be, bt in busy):
                    slots.append("blocked")
                else:
                    slots.append("available")

            # dispo_count : fenêtres de 60 min (2 slots) consécutifs "available" (non chevauchantes)
            i = 0
            while i <= len(slots) - 2:
                if slots[i] == "available" and slots[i + 1] == "available":
                    dispo_count += 1
                    i += 2
                else:
                    i += 1

            days.append({"date": day_str, "slots": slots})

        return name, {"days": days, "dispo_count": dispo_count}

    with ThreadPoolExecutor(max_workers=len(members)) as ex:
        for name, data in ex.map(_fetch_member, members):
            users_data[name] = data

    slot_labels = [f"{h:02d}:{m:02d}" for h, m in AVAIL_SLOT_TIMES]
    day_labels  = [d.strftime("%Y-%m-%d") for d in week_days]
    result = {
        "start_date":  start_day.strftime("%Y-%m-%d"),
        "end_date":    end_day.strftime("%Y-%m-%d"),
        "week_label":  f"Semaine du {start_day.day} au {end_day.day} {FR_MONTHS[end_day.month-1]} {end_day.year}",
        "slot_labels": slot_labels,
        "day_dates":   day_labels,
        "user_order":  user_order,
        "users":       users_data,
    }
    cache_set(ckey, result, ttl=120)  # 2 min — aligné avec get_busy TTL
    return result
