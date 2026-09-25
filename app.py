"""OfferDesk v2 — instant cash-offer engine for sports card dealers.

Flask + SQLite (local) or Postgres (DATABASE_URL). Accounts with Stripe
subscription gating: only emails with an active OfferDesk subscription can
sign up / log in. All dealer data (settings, deals) is scoped per user.

Run with: ./start.sh  (or: .venv/bin/python app.py)
"""
import base64
import csv
import io
import json
import os
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

from flask import (Flask, Response, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db as dbmod

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.yaml")
KEY_TXT = os.path.join(BASE, "stripe-publishable-key.txt")
STRIPE_CHECK_CLI = os.path.expanduser(
    os.environ.get("STRIPE_CHECK_CLI",
                   "~/workspace/skills/stripe/bin/subscription_check.py"))
STRIPE_PRICE_ID = os.environ.get("STRIPE_PRICE_ID", "price_1UJcVSLqZsJQhgSSB8bPpLL6")
SUB_RECHECK_HOURS = 24
SEED_USER_ID = 0  # template settings rows live under user_id 0 (never a real user)

app = Flask(__name__)
secret = os.environ.get("SECRET_KEY")
if not secret:
    secret = "dev-only-change-me"
    msg = "WARNING: SECRET_KEY env var not set — using insecure dev default!"
    print(msg)
    app.logger.warning(msg)
app.secret_key = secret
app.permanent_session_lifetime = timedelta(days=30)

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
    """Create tables; migrate legacy single-tenant SQLite DBs to per-user schema."""
    db = dbmod.connect()
    pg = dbmod.is_postgres()
    iddef = "SERIAL PRIMARY KEY" if pg else "INTEGER PRIMARY KEY AUTOINCREMENT"
    booldef = "BOOLEAN NOT NULL DEFAULT FALSE" if pg else "INTEGER NOT NULL DEFAULT 0"

    db.execute(f"""CREATE TABLE IF NOT EXISTS users (
        id {iddef}, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
        stripe_customer_id TEXT, stripe_subscription_id TEXT,
        sub_active {booldef}, sub_last_checked TEXT, created_at TEXT NOT NULL)""")

    if pg:
        db.execute("""CREATE TABLE IF NOT EXISTS settings (
            user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
            PRIMARY KEY (user_id, key))""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS deals (
            id {iddef}, user_id INTEGER NOT NULL,
            created_at TEXT, cert TEXT, card_name TEXT, grader TEXT, grade TEXT,
            comp REAL, tier_pct REAL, offer REAL,
            resale_net REAL, profit REAL, margin_pct REAL)""")
    else:
        # Migrate legacy single-tenant tables (no user_id) to the per-user schema.
        cols = [r["name"] for r in db.execute("PRAGMA table_info(settings)").fetchall()]
        if cols and "user_id" not in cols:
            db.execute("""CREATE TABLE settings_new (
                user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
                PRIMARY KEY (user_id, key))""")
            db.execute(f"INSERT INTO settings_new (user_id, key, value) "
                       f"SELECT {SEED_USER_ID}, key, value FROM settings")
            db.execute("DROP TABLE settings")
            db.execute("ALTER TABLE settings_new RENAME TO settings")
        else:
            db.execute("""CREATE TABLE IF NOT EXISTS settings (
                user_id INTEGER NOT NULL, key TEXT NOT NULL, value TEXT,
                PRIMARY KEY (user_id, key))""")
        cols = [r["name"] for r in db.execute("PRAGMA table_info(deals)").fetchall()]
        if cols and "user_id" not in cols:
            db.execute("ALTER TABLE deals ADD COLUMN user_id INTEGER")
        else:
            db.execute(f"""CREATE TABLE IF NOT EXISTS deals (
                id {iddef}, user_id INTEGER,
                created_at TEXT, cert TEXT, card_name TEXT, grader TEXT, grade TEXT,
                comp REAL, tier_pct REAL, offer REAL,
                resale_net REAL, profit REAL, margin_pct REAL)""")

    db.execute(f"""CREATE TABLE IF NOT EXISTS waitlist (
        id {iddef}, email TEXT, created_at TEXT)""")

    # Seed global template settings (user_id 0) — copied to each new user at signup.
    for k, v in DEFAULT_SETTINGS.items():
        if pg:
            db.execute("INSERT INTO settings (user_id, key, value) VALUES (0, ?, ?) "
                       "ON CONFLICT DO NOTHING", (k, v))
        else:
            db.execute("INSERT OR IGNORE INTO settings (user_id, key, value) "
                       "VALUES (0, ?, ?)", (k, v))
    db.commit()
    db.close()

def get_db():
    if "db" not in g:
        g.db = dbmod.connect()
    return g.db

@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()

def upsert_setting(uid, key, value):
    db = get_db()
    if dbmod.is_postgres():
        db.execute("""INSERT INTO settings (user_id, key, value) VALUES (?, ?, ?)
                      ON CONFLICT (user_id, key) DO UPDATE SET value=EXCLUDED.value""",
                   (uid, key, value))
    else:
        db.execute("INSERT OR REPLACE INTO settings (user_id, key, value) VALUES (?, ?, ?)",
                   (uid, key, value))

def seed_user_settings(uid):
    """Give a new user their own settings, seeded from the global templates."""
    db = get_db()
    rows = db.execute("SELECT key, value FROM settings WHERE user_id=0").fetchall()
    template = {r["key"]: r["value"] for r in rows}
    for k in DEFAULT_SETTINGS:
        upsert_setting(uid, k, template.get(k, DEFAULT_SETTINGS[k]))
    db.commit()

def get_setting(uid, key):
    row = get_db().execute(
        "SELECT value FROM settings WHERE user_id=? AND key=?", (uid, key)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key, "")

# ---------------------------------------------------------------- stripe subscription check
def _stripe_api_check(email, secret_key):
    """Direct Stripe API check (production path — never logs the key)."""
    def get(path, params):
        url = "https://api.stripe.com/v1" + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url,
                                     headers={"Authorization": f"Bearer {secret_key}"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    res = {"email": email, "active": False, "customer_id": None, "subscription_id": None}
    custs = get("/customers", {"email": email, "limit": 1}).get("data", [])
    if not custs:
        return res
    res["customer_id"] = custs[0]["id"]
    for status in ("active", "trialing"):
        subs = get("/subscriptions",
                   {"customer": res["customer_id"], "status": status, "limit": 20}).get("data", [])
        for s in subs:
            items = s.get("items", {}).get("data", [])
            if any(it.get("price", {}).get("id") == STRIPE_PRICE_ID
                   or it.get("price", {}).get("product") == STRIPE_PRICE_ID
                   for it in items):
                res.update({"active": True, "subscription_id": s["id"],
                            "status": s.get("status")})
                return res
    return res

def stripe_check(email):
    """Does this email hold an ACTIVE OfferDesk subscription?

    Returns {"active": bool, "customer_id": ..., "subscription_id": ...}.
    Uses STRIPE_SECRET_KEY directly when set (production); otherwise the
    Hatch Stripe skill CLI (local dev). Raises RuntimeError on failure.
    """
    key = os.environ.get("STRIPE_SECRET_KEY")
    if key:
        return _stripe_api_check(email, key)
    if not os.path.exists(STRIPE_CHECK_CLI):
        raise RuntimeError("no STRIPE_SECRET_KEY and subscription-check CLI not found")
    out = subprocess.run([sys.executable, STRIPE_CHECK_CLI, "--email", email,
                          "--product", STRIPE_PRICE_ID],
                         capture_output=True, text=True, timeout=45)
    if out.returncode != 0:
        raise RuntimeError("subscription check failed: %s" % (out.stderr or out.stdout)[:200])
    return json.loads(out.stdout)

# ---------------------------------------------------------------- auth
NO_SUB_MSG = ("We couldn't find an active OfferDesk subscription for this email. "
              "Subscribe first, then use the same email for your account.")
CHECK_DOWN_MSG = "Subscription check is temporarily unavailable — please try again in a minute."

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return get_db().execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def sub_stale(user):
    iso = user["sub_last_checked"]
    if not iso:
        return True
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return True
    return datetime.now() - dt > timedelta(hours=SUB_RECHECK_HOURS)

def refresh_subscription(user):
    """Re-check Stripe; persist result. Returns the check dict. Raises on failure."""
    info = stripe_check(user["email"])
    get_db().execute(
        """UPDATE users SET sub_active=?, sub_last_checked=?,
           stripe_customer_id=?, stripe_subscription_id=? WHERE id=?""",
        (bool(info.get("active")),
         datetime.now().isoformat(timespec="seconds"),
         info.get("customer_id"), info.get("subscription_id"), user["id"]))
    get_db().commit()
    return info

def kick_inactive():
    session.clear()
    flash("Your OfferDesk subscription is no longer active. "
          "Resubscribe on the pricing page to keep using the app.")
    return redirect(url_for("login"))

@app.before_request
def gate():
    """Everything except the public sales/auth surface requires a logged-in,
    actively-subscribed dealer."""
    p = request.path
    if p in ("/pricing", "/login", "/signup", "/logout") \
            or p == "/api/waitlist" or p.startswith("/static/"):
        return None
    user = current_user()
    if not user:
        nxt = p if p != "/" else ""
        return redirect(url_for("login", next=nxt) if nxt else url_for("login"))
    if sub_stale(user):
        try:
            info = refresh_subscription(user)
        except Exception as e:  # check failed — keep session, retry next time
            app.logger.warning("subscription recheck failed for %s: %s", user["email"], e)
            g.user = user
            return None
        if not info.get("active"):
            return kick_inactive()
    elif not user["sub_active"]:
        return kick_inactive()
    g.user = user
    return None

def safe_next():
    nxt = request.form.get("next") or request.args.get("next") or url_for("index")
    if not nxt.startswith("/") or nxt.startswith("//"):
        return url_for("index")
    return nxt

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        if "@" not in email or "." not in email.split("@")[-1]:
            error = "Enter a valid email."
        elif len(pw) < 8:
            error = "Password must be at least 8 characters."
        else:
            db = get_db()
            if db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone():
                error = "An account with this email already exists. Try logging in."
            else:
                try:
                    info = stripe_check(email)
                except Exception as e:
                    app.logger.warning("stripe check failed at signup for %s: %s", email, e)
                    info = None
                if info is None:
                    error = CHECK_DOWN_MSG
                elif not info.get("active"):
                    error = NO_SUB_MSG
                else:
                    now = datetime.now().isoformat(timespec="seconds")
                    uid = db.insert_id(
                        """INSERT INTO users (email, password_hash, stripe_customer_id,
                           stripe_subscription_id, sub_active, sub_last_checked, created_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (email, generate_password_hash(pw), info.get("customer_id"),
                         info.get("subscription_id"), True, now, now))
                    db.commit()
                    seed_user_settings(uid)
                    session["user_id"] = uid
                    session.permanent = True
                    return redirect(safe_next())
    return render_template("signup.html", error=error)

@app.route("/login", methods=["GET", "POST"])
def login():
    if session.get("user_id"):
        return redirect(url_for("index"))
    error = None
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        pw = request.form.get("password") or ""
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if not user or not check_password_hash(user["password_hash"], pw):
            error = "Wrong email or password."
        else:
            try:
                info = stripe_check(email)
            except Exception as e:
                app.logger.warning("stripe check failed at login for %s: %s", email, e)
                info = None
            if info is None:
                error = CHECK_DOWN_MSG
            elif not info.get("active"):
                error = NO_SUB_MSG
            else:
                db.execute(
                    """UPDATE users SET sub_active=?, sub_last_checked=?,
                       stripe_customer_id=?, stripe_subscription_id=? WHERE id=?""",
                    (True, datetime.now().isoformat(timespec="seconds"),
                     info.get("customer_id"), info.get("subscription_id"), user["id"]))
                db.commit()
                session["user_id"] = user["id"]
                session.permanent = True
                return redirect(safe_next())
    return render_template("login.html", error=error)

@app.route("/logout")
def logout():
    session.clear()
    flash("You've been signed out.")
    return redirect(url_for("login"))

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
    uid = g.user["id"]
    return render_template("index.html",
                           shop_name=get_setting(uid, "shop_name"),
                           fee_pct=get_setting(uid, "ebay_fee_pct"),
                           shipping=get_setting(uid, "default_shipping"),
                           margin_target=get_setting(uid, "margin_target_pct"))

@app.route("/pricing")
def pricing():
    cfg = load_config()
    return render_template("pricing.html",
                           payment_link=cfg.get("payment_link", ""),
                           stripe_key=cfg.get("stripe_publishable_key", ""))

@app.route("/slip/<int:deal_id>")
def slip(deal_id):
    uid = g.user["id"]
    row = get_db().execute(
        "SELECT * FROM deals WHERE id=? AND user_id=?", (deal_id, uid)).fetchone()
    if not row:
        return "Offer not found", 404
    deal = dict(row)
    tiers = compute_tiers(deal["comp"],
                          float(get_setting(uid, "ebay_fee_pct") or 13),
                          float(get_setting(uid, "default_shipping") or 0))
    return render_template("slip.html", deal=deal,
                           tiers=tiers,
                           shop_name=get_setting(uid, "shop_name"),
                           today=datetime.now().strftime("%b %d, %Y"))

# ---------------------------------------------------------------- API
@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    uid = g.user["id"]
    db = get_db()
    if request.method == "POST":
        data = request.get_json(force=True)
        for k in DEFAULT_SETTINGS:
            if k in data:
                upsert_setting(uid, k, str(data[k]))
        db.commit()
        return jsonify({"ok": True})
    return jsonify({k: get_setting(uid, k) for k in DEFAULT_SETTINGS})

@app.route("/api/offer", methods=["POST"])
def api_offer():
    uid = g.user["id"]
    data = request.get_json(force=True)
    try:
        comp = float(data.get("comp") or 0)
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid comp price."}), 400
    if comp <= 0:
        return jsonify({"error": "Enter a valid comp price."}), 400
    fee_pct = float(get_setting(uid, "ebay_fee_pct") or 13)
    shipping = float(get_setting(uid, "default_shipping") or 0)
    margin_target = float(get_setting(uid, "margin_target_pct") or 0)
    tiers = compute_tiers(comp, fee_pct, shipping)
    for t in tiers:
        t["meets_margin"] = t["margin_pct"] >= margin_target
    # save the chosen tier (default: highest tier that meets margin target, else 65)
    chosen = data.get("tier_pct")
    valid = [t for t in tiers if t["tier_pct"] == chosen]
    pick = valid[0] if valid else next(
        (t for t in reversed(tiers) if t["meets_margin"]), tiers[0])
    db = get_db()
    deal_id = db.insert_id("""INSERT INTO deals
        (user_id, created_at, cert, card_name, grader, grade, comp, tier_pct, offer,
         resale_net, profit, margin_pct)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, datetime.now().isoformat(timespec="seconds"),
         (data.get("cert") or "").strip(),
         (data.get("card_name") or "").strip(),
         (data.get("grader") or "PSA").strip(),
         (data.get("grade") or "").strip(),
         comp, pick["tier_pct"], pick["offer"],
         pick["resale_net"], pick["profit"], pick["margin_pct"]))
    db.commit()
    return jsonify({"tiers": tiers, "deal_id": deal_id, "chosen": pick})

@app.route("/api/deals")
def api_deals():
    rows = get_db().execute(
        "SELECT * FROM deals WHERE user_id=? ORDER BY id DESC", (g.user["id"],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/api/deals/<int:deal_id>", methods=["DELETE"])
def api_delete_deal(deal_id):
    db = get_db()
    db.execute("DELETE FROM deals WHERE id=? AND user_id=?", (deal_id, g.user["id"]))
    db.commit()
    return jsonify({"ok": True})

@app.route("/api/deals.csv")
def api_csv():
    rows = get_db().execute(
        "SELECT * FROM deals WHERE user_id=? ORDER BY id", (g.user["id"],)).fetchall()
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
    # Public: used by the pricing page's email capture.
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
    key = get_setting(g.user["id"], "ebay_api_key").strip()
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
