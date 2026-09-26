# 2.0.7

- A store's first challenge in a scan no longer pauses it for an hour right away: the scanner waits
  `challenge_retry_seconds` (default 90) and reloads that page once. Only a second challenge pauses the store.
- Stores that were paused get one more visit in the same slot, right after their pause ends - only those stores
  are scanned again, and the results are sent together with the other stores' results from that scan.

# 2.0.6

- The scan report now includes the pace each store started at and the pace each challenge came at (not only the pace
  at the end, which a challenge has already reset to the slow one) - for the scan log on the dashboard.

# 2.0.5

- A challenge slows the pace down immediately - for the rest of the scan, every store and product checks too -
  instead of at the end of the scan.
- Only really clean scans (no challenge, no page errors, skipped or paused stores, and pages actually read) count
  towards speeding up; a scan with errors neither speeds up nor resets the pace.

# 2.0.4

- Faster scans without a faster request rate: no extra wait between stores (the shared page queue already paces
  every request), and the Worker leaves out pages known to be unavailable except on one scan a day.
- Adaptive pace: after 3 full scans without any challenge the gap between pages shrinks by 2.5 s, down to
  `min_request_delay` (default 20 s); the first challenge puts it straight back to `request_delay`.
  Set `min_request_delay` equal to `request_delay` to keep a fixed pace.
- Each store's results are sent as soon as the store is done, so the dashboard updates during a scan.
- Scan statistics include page-load and waiting time per store and the current gap between pages.

# 2.0.3

- A dashboard scan request is served once: if its result can't be delivered it is re-sent (up to 5 times) and then
  dropped - the same request never triggers another Amazon scan; only a new request does.
- Product checks: a search result whose page didn't load is a temporary failure (the store is retried later),
  never "not sold in this store".

# 2.0.2

- A finished scan the Worker didn't accept is re-sent (never rescanned) also when the scan was requested from
  the dashboard.
- A search page that didn't load (timeout, HTTP error) is a temporary failure, never "not sold in this store".
- Product checks: a page without any model number matches only when its title clearly matches the user's link
  (two shared distinctive words); the link's own store is checked first as the reference.

# 2.0.1

- Share one paced request queue across scheduled scans, product checks, searches and seller-offer pages.
- Use the full configured delay for every request; new installations default to 30 seconds.
- Serialize scans and checks for the same store and reuse the same browser profile.
- Re-check cooldown before every request and after waiting; preserve cooldowns written by other threads.
- Seed Worker cookies only when no local browser session exists, and preserve session cookies on restart.
- These changes reduce unnecessary traffic and session resets; they do not solve an existing CAPTCHA.

# 2.0.0

- Renamed to **Amazon Price Scanner** (slug `amazon_price_scanner`, repository `ha-amazon-price-scanner`).
  Home Assistant treats it as a new add-on: install it, set the options (worker URL
  `https://amazon-price-tracker.asaf27064.workers.dev`, same upload key) and remove the old "Coffee Price Scanner".

# 1.3.0

- A single missing or slow product page no longer stops the rest of the store; two errors in a row skip the
  rest of that store (labelled "skipped", not "cooldown").
- Product checks from the dashboard run on their own thread with their own Chromium profiles, so they also work
  while a scheduled scan is running; they keep the scan's pacing between pages.
- A check that cannot read a store reports it as temporary (never as "not listed"); a CAPTCHA on a search page
  pauses the store.
- A CAPTCHA on the Amazon-offer page (`?smid=`) now pauses the store.
- A finished scan the Worker did not accept is re-sent (up to 5 times) instead of rescanning Amazon every 2 minutes.
- Sends a progress heartbeat per store, so the Worker does not start its fallback scan during a long precise scan.
- Uses the Worker's schedule per day (no extra scan at midnight when the pre-Prime / Prime schedule starts).
- Recognises combined "sold and shipped by …" seller wording.
- Chromium caches are excluded from Home Assistant backups; closing a crashed browser can no longer lose results.
- Reports its version with each scan.

# 1.2.1

- Recognise the new "Shipper / Seller Amazon" wording on amazon.com / amazon.co.uk as sold by Amazon.

# 1.2.0

- Precise "add product" checks: the dashboard queues a check and the add-on polls for it every 5 seconds.
- Checks all stores in parallel (`check_concurrency`, default 3), one page per store, verifies the match by the
  item-details model number and searches the store by model when the same ASIN is a different product.
- Reports page variations (colours/styles) and never logs page contents.
- Prefers Amazon's own offer: when a marketplace seller has the buy box, also reads the page with Amazon's
  merchant id (`?smid=`) and sends both, so the dashboard can prefer "sold by Amazon" and still show the cheaper
  marketplace offer.

# 1.1.1

- Set each marketplace's language explicitly, replacing invalid `lc-*=-` values.
- Include a short page message in diagnostics when no product is returned.

# 1.1.0

- Use Chromium with a persistent anonymous profile per Amazon store by default.
- Keep the existing requests transport available for comparison and low-memory hosts.
- Wait at least 10 seconds between product requests by default.
- Stop a store after a CAPTCHA or HTTP 403/429/503 and persist a one-hour cooldown.
- Never replace delivery cookies with cookies from a challenge or foreign-destination page.
- Diagnostics sample one product per store using the configured transport.
- Run the scanner and browser as an unprivileged user; update the Alpine base to 3.22.
