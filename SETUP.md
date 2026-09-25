# OfferDesk v1 — Setup (10 minutes)

The real, working cash-offer product. Your sales guy runs it on his phone's browser.

## What Andrew still owes (3 things)

1. **Payment link** — In Stripe: Product catalogue → your "OfferDesk Founding Dealer"
   product → Create payment link → copy it → paste into `config.yaml`
   (replace `PASTE_PAYMENT_LINK_HERE`). Until then, the pricing page shows a
   "checkout opening soon" button and collects emails to a waitlist instead.
2. **eBay API key (optional)** — Only needed for the bonus eBay reference lookup.
   Manual comp entry is the primary path and works without it. If you want it:
   developer.ebay.com → sign in → join Developers Program → create an app →
   copy the App ID → paste it in the app's Settings tab.
3. **Run it locally or deploy it** — see below. Local = free, runs on your
   computer. Deploy = public URL your sales guy can open anywhere.

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
