# Revenue Agent

An always-on, small-agent coordinator for Salee Arman: a productized AI Revenue Workflow Sprint for small businesses.

The default strategy is a $1,500 productized service: identify the highest-leverage AI workflow improvements for a small business and deliver an execution plan. The runtime can provision a Stripe-hosted Payment Link when `STRIPE_RESTRICTED_KEY` is available, serve a landing page at `/`, reply to inbound email/SMS, and follow up only with imported opted-in contacts.

## Run

```bash
cp .env.example .env
# keep your existing provider keys; fill in business settings when ready
python -m unittest discover -s tests -v
python main.py
```

The HTTP server exposes `/`, `GET /health`, `GET /metrics`, and a signed `POST /webhooks/twilio`. The server also starts the 24/7 poller by default. For a worker-only process, run `python main.py worker`.

For a restartable always-on deployment:

```bash
docker compose up -d --build
curl https://YOUR_PUBLIC_HOST/health
```

Point the Twilio incoming-message webhook at `https://YOUR_PUBLIC_HOST/webhooks/twilio` and set `PUBLIC_BASE_URL` to that exact origin.

Once the offer is configured, import only consented contacts and explicitly enable follow-ups:

```bash
python3 import_contacts.py data/contacts.example.csv
# set FOLLOW_UP_ENABLED=true in .env, then restart the service
```

## Required configuration from you

1. A Stripe live restricted key with permission to create products, prices, and payment links, or a manually created `CHECKOUT_URL`.
2. `PUBLIC_BASE_URL` (or the existing `VERCEL_URL`) set to the public HTTPS origin and `STRIPE_WEBHOOK_SECRET` for `/webhooks/stripe`.
3. A consented lead/customer source; import it with `import_contacts.py`.
4. An owner email for exceptions and fulfillment escalation.

Use a hosted payment link in `CHECKOUT_URL` initially. The agent never receives or stores card data; it only explains the configured offer and sends the configured checkout URL.

The runtime uses a deterministic router plus one bounded OpenRouter specialist call per supported conversation: support reply or sales/offer reply. SQLite makes retries and duplicate webhooks safe, while `audit_log` records every poll, decision, send, and provider error. Quality control is local and does not consume model credits.

Growth capabilities are documented in [docs/autonomous_growth_capabilities.md](docs/autonomous_growth_capabilities.md). Salee runs the growth specialist every six hours by default, publishing landing copy, FAQ/SEO/GEO metadata, and blog artifacts while recording offer pivots as experiments.
