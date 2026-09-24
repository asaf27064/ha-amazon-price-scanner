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
