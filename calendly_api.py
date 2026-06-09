import requests
import os
from datetime import datetime, timedelta
from functools import lru_cache
from concurrent.futures import ThreadPoolExecutor
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
            {"name": inv.get("name", ""), "email": inv.get("email", "")}
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
    try:
        events = api_get_all_pages(
            f"{CALENDLY_BASE}/scheduled_events",
            {"organization": org_uri, "status": "active",
             "min_start_time": start_utc, "max_start_time": end_utc}
        )
        for e in events:
            memberships = e.get("event_memberships", [])
            member_name = ""
            if memberships:
                user_uri    = memberships[0].get("user", "")
                member_name = uri_to_name.get(user_uri, memberships[0].get("user_name", ""))
            raw_events.append((member_name, e))
    except Exception as ex:
        print(f"[import] org query error: {ex}")

    def build_booking(member_name_event):
        member_name, e = member_name_event
        try:
            s = parse_dt_paris(e["start_time"])
            booking = {
                "event_uri":   e.get("uri", ""),
                "member_name": member_name,
                "event_type":  e.get("name", ""),
                "start_time":  s.strftime("%H:%M"),
                "date":        s.strftime("%Y-%m-%d"),
                "status":      "active",
                "lead_name":   "",
                "lead_email":  "",
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
