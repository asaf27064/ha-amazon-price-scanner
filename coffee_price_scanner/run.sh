#!/usr/bin/with-contenv bashio
export WORKER_URL="$(bashio::config 'worker_url')"
export UPLOAD_KEY="$(bashio::config 'upload_key')"
export IPV4_ONLY="$(bashio::config 'ipv4_only')"
bashio::log.info "Coffee price scanner starting"
exec python3 -u /scanner.py --loop
