# Coffee Price Scanner

Runs the precise Amazon scanner from your home network and sends the results to your Cloudflare
price tracker (dashboard + Telegram alerts). It checks every 2 minutes whether a scan is due
(schedule and engine are set on the dashboard) or whether "scan now" was pressed.

## Configuration
- **worker_url** – the tracker Worker address (https://…workers.dev)
- **upload_key** – the tracker's upload key (from `tracker\secrets.json` on your PC)
- **transport** – `browser` (default) uses Chromium and JavaScript, with an anonymous
  profile for each store saved under `/data/scanner/browser`. `requests` uses the old
  HTTP client for comparison. The browser uses more RAM; only one store runs at a time.
- **request_delay** – minimum seconds between products, default 10 (minimum 5).
- **ipv4_only** – restricts Python HTTP requests to IPv4. This does not change
  Chromium's networking. Leave false unless network diagnostics require it.

After an update, missing new options default to `browser` and 10 seconds.
The scanner runs as an unprivileged user inside the normal Home Assistant add-on
container. Chromium's nested namespace sandbox is disabled because HA's restricted
container does not provide the required namespace privileges. No host networking,
privileged mode, Docker socket or exposed browser-control port is needed.

On CAPTCHA or HTTP throttling, the affected store pauses for one hour, including
after restarts. A manual scan does not override that pause. The scanner never solves
CAPTCHAs automatically. Browser sessions reduce differences from desktop browsing;
they cannot guarantee that Amazon will never request verification.

Use the dashboard's diagnostic scan to test one product in each store without
updating price history. Reports include the scanner version and transport. The
regular scan logs destination, price, delivery and status, without cookie values.

On the dashboard choose **Engine → Home Assistant – precise**.
If this add-on is stopped, Cloudflare automatically runs the light scan as a fallback.

## Adding products (precise check)
When you paste a product link on the dashboard, the dashboard shows a quick check within seconds and queues a
precise check. This add-on picks it up within 5 seconds, checks all stores in parallel and the dashboard updates
the preview ("✓ verified"). `check_concurrency` (1–6, default 3) sets how many stores are checked at once –
lower it on hosts with little memory.
