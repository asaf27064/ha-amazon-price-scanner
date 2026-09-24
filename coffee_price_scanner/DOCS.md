# Coffee Price Scanner

Runs the precise Amazon scanner from your home network and sends the results to your Cloudflare
price tracker (dashboard + Telegram alerts). It checks every 2 minutes whether a scan is due
(schedule and engine are set on the dashboard) or whether "scan now" was pressed.

## Configuration
- **worker_url** – the tracker Worker address (https://…workers.dev)
- **upload_key** – the tracker's upload key (from `tracker\secrets.json` on your PC)

On the dashboard choose **Engine → Home Assistant – precise**.
If this add-on is stopped, Cloudflare automatically runs the light scan as a fallback.
