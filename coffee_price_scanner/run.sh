#!/usr/bin/with-contenv bashio
export WORKER_URL="$(bashio::config 'worker_url')"
export UPLOAD_KEY="$(bashio::config 'upload_key')"
export IPV4_ONLY="$(bashio::config 'ipv4_only')"
export TRANSPORT=browser
export REQUEST_DELAY=10
if bashio::config.has_value 'transport'; then
    export TRANSPORT="$(bashio::config 'transport')"
fi
export CHECK_CONCURRENCY=3
if bashio::config.has_value 'check_concurrency'; then
    export CHECK_CONCURRENCY="$(bashio::config 'check_concurrency')"
fi
if bashio::config.has_value 'request_delay'; then
    export REQUEST_DELAY="$(bashio::config 'request_delay')"
fi
export DATA_DIR=/data/scanner
export HOME=/data/scanner/home
mkdir -p "$DATA_DIR" "$HOME"
chown -R scanner:scanner "$DATA_DIR"
chmod 700 "$DATA_DIR" "$HOME"
bashio::log.info "Coffee price scanner starting"
exec su-exec scanner /opt/venv/bin/python -u /scanner.py --loop
