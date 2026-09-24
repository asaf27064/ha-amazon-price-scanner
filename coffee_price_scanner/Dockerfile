ARG BUILD_FROM
FROM $BUILD_FROM
# Alpine packages - no compilation needed on the Home Assistant host
RUN apk add --no-cache python3 py3-pip py3-requests py3-beautifulsoup4 py3-lxml tzdata \
    chromium chromium-chromedriver su-exec \
    && python3 -m venv --system-site-packages /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir selenium==4.49.0 \
    && addgroup -S scanner && adduser -S -G scanner scanner
COPY scanner.py /scanner.py
COPY browser_client.py /browser_client.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
