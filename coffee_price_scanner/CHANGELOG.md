# 1.1.0

- Use Chromium with a persistent anonymous profile per Amazon store by default.
- Keep the existing requests transport available for comparison and low-memory hosts.
- Wait at least 10 seconds between product requests by default.
- Stop a store after a CAPTCHA or HTTP 403/429/503 and persist a one-hour cooldown.
- Never replace delivery cookies with cookies from a challenge or foreign-destination page.
- Diagnostics sample one product per store using the configured transport.
- Run the scanner and browser as an unprivileged user; update the Alpine base to 3.22.
