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

def sb_upsert(data: dict):
    """Insère ou met à jour un booking dans Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    try:
        req_http.post(
            f"{SUPABASE_URL}/rest/v1/bookings",
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

@app.route("/api/invitees")
def invitees():
    try:
        event_uri = request.args.get("event_uri", "")
        return jsonify(calendly_api.get_event_invitees(event_uri))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        if row:
            return jsonify({
                "invitees": [{"name": row.get("lead_name", ""), "email": row.get("lead_email", "")}],
                "event":    {
                    "name":  row.get("event_type", ""),
                    "start": row.get("start_time", ""),
                    "uri":   row.get("event_uri", ""),
                },
                "source": "supabase",
            })

        # 2. API Calendly (fallback)
        result = calendly_api.get_slot_invitees(member_name, date, time)
        result["source"] = "calendly_api"
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

            sb_upsert({
                "event_uri":   scheduled.get("uri", ""),
                "member_name": closer_name,
                "lead_name":   payload.get("name", ""),
                "lead_email":  payload.get("email", ""),
                "event_type":  scheduled.get("name", ""),
                "start_time":  time_str,
                "date":        date_str,
                "status":      "active",
            })

        elif ev_type == "invitee.canceled":
            scheduled = payload.get("scheduled_event", {})
            event_uri = scheduled.get("uri", "")
            if event_uri:
                sb_set_status(event_uri, "canceled")

        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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

@app.route("/api/import_history", methods=["POST"])
def import_history():
    """Importe tous les RDV historiques (90j passés + 30j futurs) dans Supabase."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return jsonify({"error": "Supabase non configuré"}), 500
    try:
        bookings = calendly_api.get_all_bookings_for_import(days_past=90, days_future=30)
        if not bookings:
            return jsonify({"ok": True, "imported": 0})

        # Upsert en batch (Supabase accepte un tableau)
        r = req_http.post(
            f"{SUPABASE_URL}/rest/v1/bookings",
            headers=_sb_headers("resolution=merge-duplicates,return=minimal"),
            json=bookings,
            timeout=60
        )
        return jsonify({"ok": r.ok, "imported": len(bookings), "status": r.status_code})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/refresh", methods=["POST"])
def refresh():
    calendly_api.cache_clear()
    return jsonify({"ok": True})

@app.route("/api/health")
def health():
    key = os.environ.get("CALENDLY_API_KEY", "")
    return jsonify({
        "status":      "ok",
        "key_set":     bool(key),
        "key_len":     len(key),
        "supabase":    bool(SUPABASE_URL),
        "webhook_url": f"{APP_URL}/api/webhook/calendly" if APP_URL else None,
    })

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
