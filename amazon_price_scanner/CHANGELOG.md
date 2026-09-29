# 2.1.7

- Follows the site when it moves to another Cloudflare account: the old copy answers "moved to <address>" and the
  add-on switches by itself (kept in /data across restarts). Changing worker_url in the options still wins.

# 2.1.6

- Fewer requests to the site (the Cloudflare free plan counts every one): the product-check poll and the
  "scan now" check are one call every 10 seconds (was two, every 5 and 15 seconds - about 23,000 a day, now 8,600);
  scan progress is reported every 30 seconds (was 12).

# 2.1.5

- Reads Amazon's "join Prime to buy this item at X" line: the dashboard and the alerts mark members-only (Prime)
  prices, and show the guest price when Amazon shows both.

# 2.1.4

- Live scan progress for the dashboard: pages read / planned per store, the product being read, and each store's
  state (waiting, scanning, waiting 90 s after a challenge, paused, done). Reported at most every 12 seconds.
- "Scan now" (the whole scan or one product) starts within seconds: the job thread looks for a waiting request
  every 15 seconds and wakes the scan loop.

# 2.1.3

- "Scan only this product" from the dashboard: a short scan of one product in every store. It doesn't count as the
  slot's scan (the scheduled scan still runs) and plans no follow-up.

# 2.1.2

- The safe queue waits until its next moment stops moving: a second challenge while a page is waiting pushes that
  page later too.
- A challenge in a product check also makes turbo rest; the rest counts down only on scans that really read pages.
- The scan result carries its real elapsed time (with stores in parallel, summed load + wait is work, not duration).

# 2.1.1

- Turbo fallback is complete: a page that was waiting in a store's queue when a challenge came goes to the safe
  queue instead, and the first page on the safe queue waits the full safe gap.
- Pace state is saved under a lock with a private temporary file (parallel stores could collide and break the
  challenge handling).
- Partial results are sent to the site one at a time (two at once could overwrite each other there).

# 2.1.0

- Turbo mode (option `turbo`): several stores are scanned at the same time (`turbo_parallel_stores`, default 3),
  each with its own browser and its own gap (`turbo_delay`, default 0 s).
- Smart fallback: the first challenge puts the rest of that scan back on the safe path (one page at a time,
  `request_delay` apart); the challenged store gets its reload / pause / return as before. Turbo then rests for
  3 scans and tries again by itself.

# 2.0.12

- A product page that doesn't exist in a store (a variant Amazon doesn't sell there) no longer counts towards
  "two errors in a row" - so a product with many variants can't make the scanner give up on a store.

# 2.0.11

- A scan result tells the site when the scan started and which "scan now" it served, so a request pressed while a
  scan was already running is kept and served by the next run instead of being dropped.

# 2.0.10

- A follow-up sends only the stores it read again (the site keeps the others as they are).
- The product image is found on pages whose main image carries no hi-res attribute (the page's image list is used).
- The browser's client-hint architecture follows the real host CPU (amd64 / aarch64 / armv7).

# 2.0.9

- The store's home page (opened after a 3-hour break) is now its own paced, counted page, and a challenge on it
  is handled like any other (one reload, then the pause) instead of being passed over.
- A follow-up sends the earlier stores' results marked as kept, with the time they were really read; the site
  shows them with that time and counts only the newly read pages. A partial scan never counts as a clean scan.
- An undelivered result is re-sent even after the slot already counts as scanned.
- The browser's client-hint headers now agree with its user agent. The reload wait is included in the wait time.

# 2.0.8

- The browser presents itself like an ordinary desktop Chromium: no "automated software" switch or
  navigator.webdriver flag, the headless label removed from the user agent, the household's time zone, and
  common fonts installed in the image.
- Pages are reached the way a person reaches them: a store not visited for 3 hours gets its home page first, and
  the next page is opened from the current one (so it carries a referrer) instead of as a typed address.

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
