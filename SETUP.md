# OfferDesk v2 — Setup

The real, working cash-offer product. Dealers log in with accounts; only
active $49/mo Stripe subscribers get in.

## How a dealer gets access (the flow)

1. Dealer taps **Subscribe** on `/pricing` → pays via the Stripe Payment Link
   (off-site checkout — the app never touches cards).
2. Dealer opens `/signup` and creates an account with the **same email** they
   subscribed with. The app checks Stripe for an active subscription on that
   email; no subscription → no account.
3. Dealer logs in at `/login` on their phone. Done — their settings, deals,
   and history are private to their account.

## What Andrew still owes (3 things)

1. **Payment link** — already in `config.yaml` (`https://buy.stripe.com/00w9AU3N71ZD9tr9yG2go00`).
2. **Render env vars** — in the Render dashboard → offerdesk service → Environment:
   - `SECRET_KEY` — click Generate (signs login sessions; without it the app
     runs on an insecure dev default and logs a warning).
   - `STRIPE_SECRET_KEY` — your Stripe secret key (`sk_live_...`). The app uses
     it to verify subscriptions at signup/login and re-checks every 24h.
     Without it, subscription checks fail and nobody can sign up.
   - `DATABASE_URL` (recommended) — Render → New → PostgreSQL → copy the
     Internal Database URL. Without it the app uses SQLite on the instance
     disk, which is **wiped on every redeploy** on the free tier (deal history
     lost — export CSV regularly).
3. **eBay API key (optional)** — Only needed for the bonus eBay reference lookup.
   Manual comp entry is the primary path and works without it. If you want it:
   developer.ebay.com → sign in → join Developers Program → create an app →
   copy the App ID → paste it in the app's Settings tab.

## Run it locally (sales guy's phone on same Wi-Fi)

```bash
cd ~/workspace/cards-offerdesk
./start.sh
```

Then open `http://<this-computer's-ip>:5000` on the phone (e.g.
`http://192.168.1.50:5000`). The phone must be on the same Wi-Fi.

First run creates the virtual environment automatically — just wait a minute.

## Deploy it to Render (public URL)

1. Push the `cards-offerdesk` folder to a GitHub repo.
2. In Render: New → Blueprint → select the repo. `render.yaml` handles the rest.
3. Open the URL Render gives you. Paste your Stripe payment link into
   `config.yaml`, commit, and Render redeploys.

Note: SQLite deal history lives on the instance disk. On Render's free tier a
redeploy wipes it — export CSV regularly, or attach a persistent disk.

## What's inside

- `app.py` — the whole server (Flask + SQLite)
- `templates/` — mobile-first UI: Offer tab, deal History, Settings, pricing page, printable slip
- `config.yaml` — payment link + Stripe key config
- `offerdesk.db` — deals, settings, waitlist (created on first run)
- `render.yaml` — one-click Render deploy
- `start.sh` — one-command start

## The 3 things to try first

1. Open the app → tap a sample card → Calculate offer → Print slip.
2. Settings tab → set your shop name, fee %, margin target → Save.
3. History tab → Export CSV.
