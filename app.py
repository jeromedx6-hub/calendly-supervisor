from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import calendly_api
import os

# Load .env if CALENDLY_API_KEY not already set
if not os.environ.get("CALENDLY_API_KEY"):
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            content = f.read().strip()
        if "=" in content.splitlines()[0]:
            # KEY=VALUE format
            for line in content.splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip()
        else:
            # Raw token format
            os.environ["CALENDLY_API_KEY"] = content.splitlines()[0].strip()

app = Flask(__name__, static_folder="static")
CORS(app)

@app.route("/")
def index():
    return send_from_directory("static", "index.html")

@app.route("/api/week")
def week():
    try:
        offset = int(request.args.get("offset", 0))
        if offset < -10 or offset > 20:
            return jsonify({"error": "Offset hors limites"}), 400
        data = calendly_api.get_week_data(offset)
        return jsonify(data)
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
        data = calendly_api.get_next7_data()
        return jsonify(data)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/members")
def members():
    try:
        return jsonify(calendly_api.get_members())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/refresh", methods=["POST"])
def refresh():
    calendly_api.cache_clear()
    return jsonify({"ok": True})

@app.route("/api/health")
def health():
    key = os.environ.get("CALENDLY_API_KEY", "")
    return jsonify({"status": "ok", "key_set": bool(key), "key_len": len(key)})

@app.route("/api/debug")
def debug():
    import requests as req
    key = os.environ.get("CALENDLY_API_KEY", "")
    if not key:
        return jsonify({"error": "CALENDLY_API_KEY manquante"}), 500
    try:
        r = req.get("https://api.calendly.com/users/me",
                    headers={"Authorization": f"Bearer {key}"}, timeout=10)
        return jsonify({"status": r.status_code, "body": r.json()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False)
