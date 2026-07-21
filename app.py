from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import calendly_api
import os
import requests as req_http
import threading
from datetime import datetime, timedelta

# Load .env if CALENDLY_API_KEY not already set
if not os.environ.get("CALENDLY_API_KEY"):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            content = f.read().strip()
        if "=" in content.splitlines()[0]:
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
        else:
            os.environ["CALENDLY_API_KEY"] = content.splitlines()[0].strip()

app = Flask(__name__, static_folder="static")
CORS(app)

# ── Supabase config ────────────────────────────────────────────────────────────
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
APP_URL      = os.environ.get("APP_URL", "")
PARIS_OFFSET = 2

def _sb_headers(prefer=None):
    h = {
        "apikey":        SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type":  "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h

_SB_UPSERT_URL = None  # lazy-init

def _sb_upsert_url():
    return f"{SUPABASE_URL}/rest/v1/bookings?on_conflict=event_uri"

def sb_upsert(data: dict):
    """Insère ou met à jour un booking dans Supabase (upsert sur event_uri)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        req_http.post(
            _sb_upsert_url(),
            headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
            json=data, timeout=5
        )
    except Exception as e:
        print(f"[Supabase] upsert error: {e}")

def sb_set_status(event_uri: str, status: str):
    """Met à jour le statut d'un booking (active / canceled)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        req_http.patch(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers=_sb_headers("return=minimal"),
            params={"event_uri": f"eq.{event_uri}"},
            json={"status": status}, timeout=5
        )
    except Exception as e:
        print(f"[Supabase] status error: {e}")

def sb_get_booking(member_name: str, date: str, time: str):
    """Cherche un booking actif par (member_name, date, start_time)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    try:
        r = req_http.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params={
                "member_name": f"eq.{member_name}",
                "date":        f"eq.{date}",
                "start_time":  f"eq.{time}",
                "status":      "eq.active",
                "select":      "lead_name,lead_email,event_type,start_time,date,event_uri",
                "limit":       "1",
            },
            timeout=5
        )
        if r.ok:
            rows = r.json()
            return rows[0] if rows else None
    except Exception as e:
        print(f"[Supabase] get error: {e}")
    return None

# ── Enregistrement webhook Calendly (au démarrage) ────────────────────────────
def _register_webhook():
    if not APP_URL:
        print("[Webhook] APP_URL non définie — skip")
        return
    try:
        org_info    = calendly_api.get_org_info()
        org_uri     = org_info.get("org_uri", "")
        if not org_uri:
            print("[Webhook] org_uri introuvable — skip")
            return

        webhook_url = f"{APP_URL}/api/webhook/calendly"

        # Vérifie si déjà enregistré
        existing = req_http.get(
            f"{calendly_api.CALENDLY_BASE}/webhook_subscriptions",
            headers=calendly_api.headers(),
            params={"organization": org_uri, "scope": "organization"},
            timeout=10
        ).json()
        for sub in existing.get("collection", []):
            if sub.get("callback_url") == webhook_url:
                print(f"[Webhook] déjà enregistré : {webhook_url}")
                return

        # Crée le webhook
        r = req_http.post(
            f"{calendly_api.CALENDLY_BASE}/webhook_subscriptions",
            headers=calendly_api.headers(),
            json={
                "url":          webhook_url,
                "events":       ["invitee.created", "invitee.canceled"],
                "organization": org_uri,
                "scope":        "organization",
            },
            timeout=10
        )
        print(f"[Webhook] enregistré ({r.status_code}) : {webhook_url}")
    except Exception as e:
        print(f"[Webhook] erreur : {e}")

# Lance l'enregistrement en background sans bloquer le démarrage
threading.Thread(target=_register_webhook, daemon=True).start()

# ── Vérification périodique Calendly (nouveaux membres / event types) ──────────
_known_members     = set()
_known_event_types = set()

def _check_calendly_updates():
    """Tourne toutes les heures. Invalide le cache si nouveaux membres ou event types détectés."""
    global _known_members, _known_event_types
    import time
    time.sleep(30)  # attendre que l'app soit bien démarrée
    while True:
        try:
            members     = calendly_api.get_members()
            member_set  = {m["name"] for m in members}
            event_types = calendly_api.get_event_types()
            et_set      = {e.get("name", "") for e in event_types}

            changed = False
            if _known_members and member_set != _known_members:
                new_m = member_set - _known_members
                gone  = _known_members - member_set
                print(f"[AutoCheck] Nouveaux closers : {new_m} | Partis : {gone}")
                changed = True
            if _known_event_types and et_set != _known_event_types:
                new_e = et_set - _known_event_types
                print(f"[AutoCheck] Nouveaux event types : {new_e}")
                changed = True

            _known_members     = member_set
            _known_event_types = et_set

            if changed:
                calendly_api.cache_clear()
                print("[AutoCheck] Cache invalidé — données rechargées.")

        except Exception as ex:
            print(f"[AutoCheck] erreur : {ex}")

        time.sleep(3600)  # vérifier toutes les heures

threading.Thread(target=_check_calendly_updates, daemon=True).start()

# ── Sync Supabase automatique (toutes les 6h) ─────────────────────────────────
_last_auto_sync = None

def _run_supabase_sync():
    """Réimporte 90j passés + 30j futurs dans Supabase. Appelé par le thread auto-sync."""
    global _last_auto_sync
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        bookings = calendly_api.get_all_bookings_for_import(days_past=180, days_future=30)
        seen, unique = set(), []
        for b in bookings:
            k = b.get("event_uri", "")
            if k and k not in seen:
                seen.add(k); unique.append(b)
        CHUNK = 100
        for i in range(0, len(unique), CHUNK):
            req_http.post(
                _sb_upsert_url(),
                headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
                json=unique[i:i+CHUNK], timeout=30
            )
        _last_auto_sync = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        print(f"[AutoSync] ✅ {len(unique)} bookings sync — {_last_auto_sync}")
    except Exception as ex:
        print(f"[AutoSync] ❌ erreur : {ex}")

def _auto_sync_loop():
    import time
    time.sleep(30)           # laisser Flask démarrer (30s suffisent)
    _run_supabase_sync()     # sync immédiat au démarrage → comble les webhooks ratés
    while True:
        time.sleep(2 * 3600) # sync toutes les 2h (au lieu de 6h)
        _run_supabase_sync()

threading.Thread(target=_auto_sync_loop, daemon=True).start()

# ── Flux temps réel : dernières prises de RDV reçues par webhook ──────────────
from collections import deque
_recent_bookings = deque(maxlen=50)   # 50 derniers RDVs reçus
_invitee_url_cache = {}               # event_uri → {cancel_url, reschedule_url}

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/week")
def week():
    try:
        offset = int(request.args.get("offset", 0))
        if offset < -10 or offset > 20:
            return jsonify({"error": "Offset hors limites"}), 400
        return jsonify(calendly_api.get_week_data(offset))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/activity")
def activity():
    try:
        return jsonify(calendly_api.get_activity_status())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/diagnostic")
def diagnostic():
    try:
        return jsonify(calendly_api.get_diagnostic())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/next7")
def next7():
    try:
        return jsonify(calendly_api.get_next7_data())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/events")
def events():
    try:
        offset = int(request.args.get("offset", 0))
        if offset < -10 or offset > 20:
            return jsonify({"error": "Offset hors limites"}), 400
        return jsonify(calendly_api.get_events_week_data(offset))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/events/next7")
def events_next7():
    try:
        return jsonify(calendly_api.get_events_next7_data())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/export_bookings")
def export_bookings():
    """Exporte tous les bookings Supabase en CSV. Filtres optionnels: event_type, member_name, date_from, date_to."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        import csv, io
        params = {
            "status": "eq.active",
            "select": "date,start_time,member_name,lead_name,lead_email,event_type,event_uri,created_at",
            "order":  "date.desc,start_time.desc",
            "limit":  "10000",
        }
        et   = request.args.get("event_type", "").strip()
        mn   = request.args.get("member_name", "").strip()
        dfrom = request.args.get("date_from", "").strip()
        dto   = request.args.get("date_to", "").strip()
        if et:    params["event_type"]  = f"eq.{et}"
        if mn:    params["member_name"] = f"eq.{mn}"
        if dfrom: params["date"]        = f"gte.{dfrom}"
        if dto:   params["date"]        = params.get("date", "") or f"lte.{dto}"

        r = req_http.get(f"{SUPABASE_URL}/rest/v1/bookings", headers=_sb_headers(), params=params, timeout=20)
        bookings = r.json() if r.ok else []

        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=["date","start_time","lead_name","lead_email","event_type","member_name","event_uri","created_at"], extrasaction="ignore")
        writer.writeheader()
        writer.writerows(bookings)
        csv_bytes = buf.getvalue().encode("utf-8-sig")  # BOM pour Excel

        from flask import Response
        return Response(
            csv_bytes,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename=bookings_{datetime.utcnow().strftime('%Y%m%d_%H%M')}.csv"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/search_leads")
def search_leads():
    """Recherche de prospects par nom ou email dans Supabase."""
    try:
        q = request.args.get("q", "").strip()
        if not q or len(q) < 2:
            return jsonify({"results": [], "count": 0})
        if not SUPABASE_URL or not SUPABASE_KEY:
            return jsonify({"error": "Supabase non configuré", "results": []}), 200
        r = req_http.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers=_sb_headers(),
            params={
                "or":     f"(lead_name.ilike.*{q}*,lead_email.ilike.*{q}*,member_name.ilike.*{q}*)",
                "select": "date,start_time,member_name,event_type,lead_name,lead_email,event_uri,status,created_at",
                "order":  "date.desc",
                "limit":  "200",
            },
            timeout=10
        )
        results = r.json() if r.ok else []
        # Dédupliquer par event_uri
        seen, unique = set(), []
        for b in results:
            k = b.get("event_uri", "")
            if k not in seen:
                seen.add(k); unique.append(b)
        return jsonify({"results": unique, "count": len(unique)})
    except Exception as e:
        return jsonify({"error": str(e), "results": []}), 500

@app.route("/api/invitees")
def invitees():
    try:
        event_uri = request.args.get("event_uri", "")
        return jsonify(calendly_api.get_event_invitees(event_uri))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/invitee_actions")
def invitee_actions():
    """Retourne cancel_url + reschedule_url pour un event_uri donné."""
    try:
        event_uri = request.args.get("event_uri", "")
        if not event_uri:
            return jsonify({"cancel_url": "", "reschedule_url": ""})
        # 1. Cache webhook (instantané, sans appel API)
        if event_uri in _invitee_url_cache:
            return jsonify(_invitee_url_cache[event_uri])
        # 2. Cache Calendly invitees (TTL 30 min)
        inv = calendly_api.get_event_invitees(event_uri)
        first = inv[0] if inv else {}
        result = {
            "cancel_url":     first.get("cancel_url", ""),
            "reschedule_url": first.get("reschedule_url", ""),
        }
        # Mettre en cache si on a les URLs
        if result["cancel_url"] or result["reschedule_url"]:
            _invitee_url_cache[event_uri] = result
        return jsonify(result)
    except Exception as e:
        return jsonify({"cancel_url": "", "reschedule_url": "", "error": str(e)})

@app.route("/api/stats")
def stats_data():
    """Retourne les bookings Supabase pour la page statistiques.
    Pagine par chunks de 1000 pour contourner la limite Supabase (plan gratuit = 1000 lignes/requête)."""
    try:
        if not SUPABASE_URL or not SUPABASE_KEY:
            return jsonify({"error": "Supabase non configuré", "bookings": []}), 200
        months_back = min(int(request.args.get("months", 6)), 24)
        start_date  = (datetime.now() - timedelta(days=months_back * 31)).strftime("%Y-%m-%d")

        PAGE = 1000
        all_bookings = []
        offset = 0
        while True:
            r = req_http.get(
                f"{SUPABASE_URL}/rest/v1/bookings",
                headers={**_sb_headers(), "Range-Unit": "items",
                         "Range": f"{offset}-{offset + PAGE - 1}"},
                params={
                    "date":   f"gte.{start_date}",
                    "status": "eq.active",
                    "select": "date,member_name,event_type,start_time,created_at",
                    "order":  "date.asc",
                },
                timeout=15
            )
            if not r.ok:
                break
            chunk = r.json()
            if not chunk:
                break
            all_bookings.extend(chunk)
            if len(chunk) < PAGE:
                break          # dernière page
            offset += PAGE

        return jsonify({"bookings": all_bookings, "count": len(all_bookings), "start_date": start_date})
    except Exception as e:
        return jsonify({"error": str(e), "bookings": []}), 500

@app.route("/api/slot_invitees")
def slot_invitees():
    """
    Retourne les invités pour un créneau donné.
    1. Cherche d'abord dans Supabase (rapide, fiable).
    2. Fallback sur l'API Calendly si non trouvé.
    """
    try:
        member_name = request.args.get("member_name", "")
        date        = request.args.get("date", "")
        time        = request.args.get("time", "")

        # 1. Supabase
        row = sb_get_booking(member_name, date, time)
        if row and row.get("lead_name"):
            return jsonify({
                "invitees": [{"name": row.get("lead_name", ""), "email": row.get("lead_email", "")}],
                "event":    {
                    "name":  row.get("event_type", ""),
                    "start": row.get("start_time", ""),
                    "uri":   row.get("event_uri", ""),
                },
                "source": "supabase",
            })

        # 2. API Calendly (fallback — aussi si lead_name vide dans Supabase)
        result = calendly_api.get_slot_invitees(member_name, date, time)
        result["source"] = "calendly_api"
        # Si Supabase avait la row mais pas le lead_name, enrichir avec event_type
        if row and not result.get("event"):
            result["event"] = {
                "name":  row.get("event_type", ""),
                "start": row.get("start_time", ""),
                "uri":   row.get("event_uri", ""),
            }
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/webhook/calendly", methods=["POST"])
def webhook_calendly():
    """Reçoit les événements Calendly et les stocke dans Supabase."""
    try:
        data    = request.get_json(force=True)
        ev_type = data.get("event", "")
        payload = data.get("payload", {})

        if ev_type == "invitee.created":
            scheduled   = payload.get("scheduled_event", {})
            memberships = scheduled.get("event_memberships", [])
            closer_name = memberships[0].get("user_name", "") if memberships else ""

            start_utc = scheduled.get("start_time", "")
            if start_utc:
                dt       = datetime.fromisoformat(start_utc.replace("Z", "+00:00")).replace(tzinfo=None)
                dt_paris = dt + timedelta(hours=PARIS_OFFSET)
                date_str = dt_paris.strftime("%Y-%m-%d")
                time_str = dt_paris.strftime("%H:%M")
            else:
                date_str = time_str = ""

            booking = {
                "event_uri":    scheduled.get("uri", ""),
                "member_name":  closer_name,
                "lead_name":    payload.get("name", ""),
                "lead_email":   payload.get("email", ""),
                "event_type":   scheduled.get("name", ""),
                "start_time":   time_str,
                "date":         date_str,
                "status":       "active",
                "received_at":  datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "created_at":   payload.get("created_at", datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")),
            }
            # URLs d'action : stocker en mémoire + cache dédié
            c_url = payload.get("cancel_url", "")
            r_url = payload.get("reschedule_url", "")
            booking["_cancel_url"]     = c_url
            booking["_reschedule_url"] = r_url
            event_uri_key = booking.get("event_uri", "")
            if event_uri_key:
                _invitee_url_cache[event_uri_key] = {"cancel_url": c_url, "reschedule_url": r_url}
            sb_upsert(booking)
            _recent_bookings.appendleft(booking)  # le plus récent en tête

        elif ev_type == "invitee.canceled":
            scheduled = payload.get("scheduled_event", {})
            event_uri = scheduled.get("uri", "")
            if event_uri:
                sb_set_status(event_uri, "canceled")

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/recent_bookings")
def recent_bookings():
    """Retourne les 50 derniers RDVs.
    - Si webhooks en mémoire : les retourne en priorité (ont received_at précis)
    - Sinon fallback Supabase : derniers bookings par date/heure desc
    - Toujours enrichit avec les bookings Supabase des 7 derniers jours
    """
    mem = list(_recent_bookings)  # webhooks reçus depuis le démarrage

    # Fallback / complément Supabase : 50 dernières PRISES de RDV (par date de réservation)
    sb_bookings = []
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            r = req_http.get(
                f"{SUPABASE_URL}/rest/v1/bookings",
                headers=_sb_headers(),
                params={
                    "status": "eq.active",
                    "select": "event_uri,member_name,lead_name,lead_email,event_type,date,start_time,created_at",
                    "order":  "created_at.desc",
                    "limit":  "50",
                },
                timeout=8
            )
            if r.ok:
                raw = r.json()
                for b in raw:
                    if b.get("created_at") and not b.get("received_at"):
                        b["received_at"] = b["created_at"]
                sb_bookings = raw
        except Exception:
            pass

    # Fusionner : webhooks (avec received_at) en tête, puis Supabase pour compléter
    mem_uris = {b.get("event_uri") for b in mem}
    for b in sb_bookings:
        if b.get("event_uri") not in mem_uris:
            mem.append(b)

    # Normaliser les clés _cancel_url / _reschedule_url (depuis webhooks en mémoire)
    out = []
    for b in mem[:50]:
        entry = dict(b)
        entry["cancel_url"]     = b.get("_cancel_url", "") or b.get("cancel_url", "")
        entry["reschedule_url"] = b.get("_reschedule_url", "") or b.get("reschedule_url", "")
        out.append(entry)
    return jsonify({"bookings": out, "count": len(out)})

@app.route("/api/event_type_names")
def event_type_names():
    try:
        return jsonify(calendly_api.get_all_event_type_names())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/members")
def members():
    try:
        return jsonify(calendly_api.get_members())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/enrich_leads", methods=["POST"])
def enrich_leads():
    """Re-fetche les invités Calendly pour tous les bookings sans lead_email dans Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        # 1. Récupérer tous les bookings sans lead_email
        r = req_http.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers=_sb_headers(),
            params={
                "lead_email": "eq.",
                "status":     "eq.active",
                "select":     "id,event_uri,lead_name,lead_email",
                "limit":      "2000",
            },
            timeout=15
        )
        if not r.ok:
            return jsonify({"error": f"Supabase GET {r.status_code}"}), 500
        empty_rows = [row for row in r.json() if row.get("event_uri")]
        total      = len(empty_rows)
        enriched   = 0
        errors     = 0

        from concurrent.futures import ThreadPoolExecutor, as_completed

        def enrich_one(row):
            uri      = row.get("event_uri", "")
            if not uri: return False
            invitees = calendly_api.get_event_invitees(uri)
            if not invitees: return False
            inv  = invitees[0]
            name = inv.get("name", "")
            mail = inv.get("email", "")
            if not name and not mail: return False
            # PATCH Supabase
            patch = req_http.patch(
                f"{SUPABASE_URL}/rest/v1/bookings",
                headers=_sb_headers(prefer="return=minimal"),
                params={"event_uri": f"eq.{uri}"},
                json={"lead_name": name, "lead_email": mail},
                timeout=8
            )
            return patch.ok

        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = {ex.submit(enrich_one, row): row for row in empty_rows}
            for fut in as_completed(futures):
                try:
                    if fut.result(): enriched += 1
                    else: errors += 1
                except Exception:
                    errors += 1

        return jsonify({"ok": True, "total_empty": total, "enriched": enriched, "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/import_history", methods=["POST"])
def import_history():
    """Importe tous les RDV historiques dans Supabase. days_past param (défaut 90, max 365)."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        days_past = min(int(request.args.get("days_past", 90)), 365)
        bookings = calendly_api.get_all_bookings_for_import(days_past=days_past, days_future=30)
        if not bookings:
            return jsonify({"ok": True, "imported": 0})

        # Dédupliquer par event_uri (évite les 409 liés aux doublons intra-batch)
        seen = set()
        unique = []
        for b in bookings:
            k = b.get("event_uri", "")
            if k and k not in seen:
                seen.add(k)
                unique.append(b)

        # Upsert en chunks de 100 pour éviter les timeouts et conflits
        CHUNK = 100
        errors = []
        for i in range(0, len(unique), CHUNK):
            chunk = unique[i:i+CHUNK]
            r = req_http.post(
                _sb_upsert_url(),
                headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
                json=chunk,
                timeout=30
            )
            if not r.ok:
                errors.append({"chunk": i // CHUNK, "status": r.status_code, "body": r.text[:200]})

        return jsonify({"ok": len(errors) == 0, "imported": len(unique), "errors": errors})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/refresh", methods=["POST"])
def refresh():
    calendly_api.cache_clear()
    return jsonify({"ok": True})

def sb_get_week_bookings(start_str: str, end_str: str) -> dict:
    """Lit les bookings actifs de la semaine depuis Supabase. Retourne {member_name: [booking...]}."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return {}
    try:
        r = req_http.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers=_sb_headers(),
            params={
                "date":   f"gte.{start_str}",
                "status": "eq.active",
                "select": "date,start_time,event_type,member_name,lead_name",
                "limit":  "1000",
                "order":  "date.asc,start_time.asc",
            },
            timeout=8,
        )
        rows = [b for b in (r.json() if r.ok else []) if b.get("date", "") <= end_str]
        by_member: dict = {}
        for b in rows:
            by_member.setdefault(b.get("member_name", ""), []).append(b)
        return by_member
    except Exception as e:
        print(f"[sb_week_bookings] {e}")
        return {}


@app.route("/api/availability_week")
@app.route("/api/closer_availability")  # rétrocompatibilité
def availability_week():
    """Disponibilité hebdomadaire : bookings depuis Supabase + schedule/busy Calendly."""
    try:
        start_str = request.args.get("start_date", "")
        if start_str:
            start_day = datetime.strptime(start_str, "%Y-%m-%d")
        else:
            today = datetime.utcnow() + timedelta(hours=PARIS_OFFSET)
            start_day = today - timedelta(days=today.weekday())
        start_day = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        end_day   = start_day + timedelta(days=6)

        # Bookings depuis Supabase (1 requête HTTP rapide, pas d'API Calendly)
        bookings_by_member = sb_get_week_bookings(
            start_day.strftime("%Y-%m-%d"),
            end_day.strftime("%Y-%m-%d"),
        )
        data = calendly_api.build_availability_week_v2(start_day, bookings_by_member)
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/health")
def health():
    key = os.environ.get("CALENDLY_API_KEY", "")
    return jsonify({
        "status":        "ok",
        "key_set":       bool(key),
        "key_len":       len(key),
        "supabase":      bool(SUPABASE_URL),
        "webhook_url":   f"{APP_URL}/api/webhook/calendly" if APP_URL else None,
        "last_auto_sync": _last_auto_sync,
    })

@app.route("/api/event_available_times")
def event_available_times():
    """Retourne les vrais créneaux Calendly pour un event type sur une semaine.
    Accepte event_type_uris (CSV) ou event_type_uri (single) pour le round-robin."""
    try:
        # Accepte une liste séparée par virgules (round-robin) OU un seul URI
        uris_csv = request.args.get("event_type_uris", "") or request.args.get("event_type_uri", "")
        week_offset = int(request.args.get("week_offset", 0))
        next7       = request.args.get("next7", "false") == "true"
        if not uris_csv:
            return jsonify({"error": "event_type_uris requis"}), 400
        uris = [u.strip() for u in uris_csv.split(",") if u.strip()]

        if next7:
            start_day = (datetime.utcnow() + timedelta(hours=PARIS_OFFSET)).replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            start_day = calendly_api.BASE_MONDAY + timedelta(weeks=week_offset)

        end_day   = start_day + timedelta(days=6)
        start_utc = (start_day - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT00:00:00.000000Z")
        end_utc   = (end_day   - timedelta(hours=PARIS_OFFSET)).strftime("%Y-%m-%dT23:59:59.000000Z")

        slots = calendly_api.get_event_type_available_times(uris, start_utc, end_utc)
        return jsonify(slots)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug_avail")
def debug_avail():
    """Debug brut de l'endpoint event_type_available_times Calendly."""
    event_type_uri = request.args.get("uri", "")
    if not event_type_uri:
        # Utilise le premier URI de Mon Passage à l'action par défaut
        try:
            names = calendly_api.get_all_event_type_names()
            mpa = next((e for e in names if "Passage" in e["name"]), None)
            if mpa and mpa.get("uris"):
                event_type_uri = mpa["uris"][0]
        except Exception:
            pass
    now = datetime.utcnow()
    start_utc = now.strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    end_utc   = (now + timedelta(days=6)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    results = []
    uris = [u.strip() for u in event_type_uri.split(",") if u.strip()] if event_type_uri else []
    for uri in uris[:3]:  # max 3 pour debug
        try:
            r = req_http.get(
                f"{calendly_api.CALENDLY_BASE}/event_type_available_times",
                headers=calendly_api.headers(),
                params={"event_type": uri, "start_time": start_utc, "end_time": end_utc},
                timeout=15
            )
            body = r.json()
            results.append({"uri": uri.split("/")[-1], "status": r.status_code,
                             "count": len(body.get("collection", [])),
                             "error": body.get("message", body.get("title", "")) if not r.ok else None,
                             "sample": body.get("collection", [])[:2]})
        except Exception as e:
            results.append({"uri": uri.split("/")[-1], "error": str(e)})
    return jsonify({"start": start_utc, "end": end_utc, "results": results})

@app.route("/api/test_supabase")
def test_supabase():
    """Teste la connectivité Supabase depuis Railway — diagnostic DNS/auth."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"error": "Supabase non configuré (variables manquantes)", "url_used": SUPABASE_URL})
    try:
        r = req_http.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params={"limit": "1", "select": "id"},
            timeout=8
        )
        return jsonify({"status": r.status_code, "ok": r.ok, "body": r.text[:300], "url_used": SUPABASE_URL})
    except Exception as e:
        return jsonify({"error": str(e), "url_used": SUPABASE_URL})

@app.route("/api/bookings_for_date")
def bookings_for_date():
    """Retourne tous les bookings actifs pour une date donnée depuis Supabase."""
    date = request.args.get("date", "")
    if not date:
        return jsonify({"error": "param date requis (YYYY-MM-DD)"}), 400
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        r = req_http.get(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
            params={
                "date":   f"eq.{date}",
                "status": "eq.active",
                "select": "member_name,lead_name,lead_email,event_type,start_time,date",
                "order":  "start_time",
            },
            timeout=8
        )
        return jsonify({"bookings": r.json() if r.ok else [], "status": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug_schedules")
def debug_schedules():
    try:
        members = calendly_api.get_members()
        result = {}
        for m in members:
            try:
                data = calendly_api.api_get(f"{calendly_api.CALENDLY_BASE}/user_availability_schedules", {"user": m["uri"]})
                schedules = data.get("collection", [])
                default = next((s for s in schedules if s.get("default")), schedules[0] if schedules else None)
                result[m["name"]] = {
                    "timezone": default.get("timezone") if default else None,
                    "rules_sample": [{"wday": r.get("wday"), "intervals": r.get("intervals")} for r in (default.get("rules", [])[:3] if default else [])]
                }
            except Exception as ex:
                result[m["name"]] = {"error": str(ex)}
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/debug")
def debug():
    key = os.environ.get("CALENDLY_API_KEY", "")
    if not key:
        return jsonify({"error": "CALENDLY_API_KEY manquante"}), 500
    try:
        r = req_http.get("https://api.calendly.com/users/me",
                         headers={"Authorization": f"Bearer {key}"}, timeout=10)
        return jsonify({"status": r.status_code, "body": r.json()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
