"""OfferDesk v1 — instant cash-offer engine for sports card dealers.
Single-file Flask + SQLite app. Run with: ./start.sh  (or: .venv/bin/python app.py)
"""
import base64
import csv
import io
import json
import os
import sqlite3
import urllib.parse
import urllib.request
from datetime import datetime

from flask import Flask, g, jsonify, render_template, request, Response

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "offerdesk.db")
CONFIG_PATH = os.path.join(BASE, "config.yaml")
KEY_TXT = os.path.join(BASE, "stripe-publishable-key.txt")

app = Flask(__name__)

# ---------------------------------------------------------------- config
def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        import yaml
        with open(CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
    # Stripe publishable key: prefer config.yaml, fall back to the saved txt file
    if not cfg.get("stripe_publishable_key") and os.path.exists(KEY_TXT):
        with open(KEY_TXT) as f:
            cfg["stripe_publishable_key"] = f.read().strip()
    return cfg

# ---------------------------------------------------------------- database
DEFAULT_SETTINGS = {
    "shop_name": "My Card Shop",
    "ebay_fee_pct": "13.0",
    "default_shipping": "5.00",
    "margin_target_pct": "20.0",
    "ebay_api_key": "",
}

def init_db():
    db = sqlite3.connect(DB)
    db.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("""CREATE TABLE IF NOT EXISTS deals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT, cert TEXT, card_name TEXT, grader TEXT, grade TEXT,
        comp REAL, tier_pct REAL, offer REAL,
        resale_net REAL, profit REAL, margin_pct REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS waitlist (
        id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT, created_at TEXT)""")
    for k, v in DEFAULT_SETTINGS.items():
        db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    db.commit()
    db.close()

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def get_setting(key):
    row = get_db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

# ---------------------------------------------------------------- offer math
def compute_tiers(comp, fee_pct, shipping):
    """Return list of dicts for 65/70/75% tiers with net-of-fees math."""
    resale_net = comp * (1 - fee_pct / 100.0) - shipping
    tiers = []
    for t in (65, 70, 75):
        offer = round(comp * t / 100.0, 2)
        profit = round(resale_net - offer, 2)
        margin = round(profit / resale_net * 100, 1) if resale_net > 0 else 0.0
        tiers.append({
            "tier_pct": t,
            "offer": offer,
            "resale_net": round(resale_net, 2),
            "profit": profit,
            "margin_pct": margin,
        })
    return tiers

# ---------------------------------------------------------------- pages
@app.route("/")
def index():
    return render_template("index.html",
                           shop_name=get_setting("shop_name"),
                           fee_pct=get_setting("ebay_fee_pct"),
                           shipping=get_setting("default_shipping"),
                           margin_target=get_setting("margin_target_pct"))

@app.route("/pricing")
def pricing():
    cfg = load_config()
    return render_template("pricing.html",
                           payment_link=cfg.get("payment_link", ""),
                           stripe_key=cfg.get("stripe_publishable_key", ""))

@app.route("/slip/<int:deal_id>")
def slip(deal_id):
    row = get_db().execute("SELECT * FROM deals WHERE id=?", (deal_id,)).fetchone()
    if not row:
        return "Offer not found", 404
    deal = dict(row)
    tiers = compute_tiers(deal["comp"],
                          float(get_setting("ebay_fee_pct") or 13),
                          float(get_setting("default_shipping") or 0))
    return render_template("slip.html", deal=deal,
                           tiers=tiers,
                           shop_name=get_setting("shop_name"),
                           today=datetime.now().strftime("%b %d, %Y"))

# ---------------------------------------------------------------- API
@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True)
        for k in DEFAULT_SETTINGS:
            if k in data:
                db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                           (k, str(data[k])))
        db.commit()
        return jsonify({"ok": True})
    return jsonify({k: get_setting(k) for k in DEFAULT_SETTINGS})

@app.route("/api/offer", methods=["POST"])
def api_offer():
    data = request.get_json(force=True)
    try:
        comp = float(data.get("comp") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid comp price."}), 400
    if comp <= 0:
        return jsonify({"error": "Enter a valid comp price."}), 400
    fee_pct = float(get_setting("ebay_fee_pct") or 13)
    shipping = float(get_setting("default_shipping") or 0)
    margin_target = float(get_setting("margin_target_pct") or 0)
    tiers = compute_tiers(comp, fee_pct, shipping)
    for t in tiers:
        t["meets_margin"] = t["margin_pct"] >= margin_target
    # save the chosen tier (default: highest tier that meets margin target, else 65)
    chosen = data.get("tier_pct")
    valid = [t for t in tiers if t["tier_pct"] == chosen]
    pick = valid[0] if valid else next(
        (t for t in reversed(tiers) if t["meets_margin"]), tiers[0])
    db = get_db()
    cur = db.execute("""INSERT INTO deals
        (created_at, cert, card_name, grader, grade, comp, tier_pct, offer,
         resale_net, profit, margin_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (datetime.now().isoformat(timespec="seconds"),
         (data.get("cert") or "").strip(),
         (data.get("card_name") or "").strip(),
         (data.get("grader") or "PSA").strip(),
         (data.get("grade") or "").strip(),
         comp, pick["tier_pct"], pick["offer"],
         pick["resale_net"], pick["profit"], pick["margin_pct"]))
    db.commit()
    return jsonify({"tiers": tiers, "deal_id": cur.lastrowid, "chosen": pick})

@app.route("/api/deals")
def api_deals():
    rows = get_db().execute("SELECT * FROM deals ORDER BY id DESC").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/deals/<int:deal_id>", methods=["DELETE"])
def api_delete_deal(deal_id):
    db = get_db()
    db.execute("DELETE FROM deals WHERE id=?", (deal_id,))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/deals.csv")
def api_csv():
    rows = get_db().execute("SELECT * FROM deals ORDER BY id").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "created_at", "cert", "card_name", "grader", "grade",
                "comp", "tier_pct", "offer", "resale_net", "profit", "margin_pct"])
    for r in rows:
        w.writerow([r["id"], r["created_at"], r["cert"], r["card_name"], r["grader"],
                    r["grade"], r["comp"], r["tier_pct"], r["offer"],
                    r["resale_net"], r["profit"], r["margin_pct"]])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=offerdesk-deals.csv"})

@app.route("/api/waitlist", methods=["POST"])
def api_waitlist():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip()
    if "@" not in email:
        return jsonify({"error": "Enter a valid email."}), 400
    db = get_db()
    db.execute("INSERT INTO waitlist (email, created_at) VALUES (?, ?)",
               (email, datetime.now().isoformat(timespec="seconds")))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/ebay-comps")
def api_ebay_comps():
    """Bonus: reference comps from eBay active listings (Browse API).
    Manual comp entry is the primary path — this is a convenience lookup."""
    key = get_setting("ebay_api_key").strip()
    q = (request.args.get("q") or "").strip()
    if not key:
        return jsonify({"error": "no_key",
                        "message": "Add your free eBay Developer API key in Settings to enable this lookup. Manual comp entry works without it."})
    if not q:
        return jsonify({"error": "Enter a search term."}), 400
    try:
        # client-credentials token
        creds = base64.b64encode(f"{key}:".encode()).decode()
        tok_req = urllib.request.Request(
            "https://api.ebay.com/identity/v1/oauth2/token",
            data=urllib.parse.urlencode(
                {"grant_type": "client_credentials",
                 "scope": "https://api.ebay.com/oauth/api_scope"}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded",
                     "Authorization": f"Basic {creds}"})
        tok = json.loads(urllib.request.urlopen(tok_req, timeout=15).read())
        access = tok["access_token"]
        url = ("https://api.ebay.com/buy/browse/v1/item_summary/search?"
               + urllib.parse.urlencode({"q": q, "limit": 10, "sort": "price"}))
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access}"})
        res = json.loads(urllib.request.urlopen(req, timeout=15).read())
        items = []
        for it in res.get("itemSummaries", []):
            price = (it.get("price") or {}).get("value")
            if price:
                items.append({"title": it.get("title", "")[:80],
                              "price": float(price),
                              "url": (it.get("itemWebUrl") or "")})
        return jsonify({"items": items,
                        "note": "Active eBay listings for reference — not sold prices."})
    except Exception as e:  # noqa: BLE001 — surface as friendly error
        return jsonify({"error": "ebay_error",
                        "message": f"eBay lookup failed ({e}). Use manual comp entry."}), 502

# ---------------------------------------------------------------- main
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
