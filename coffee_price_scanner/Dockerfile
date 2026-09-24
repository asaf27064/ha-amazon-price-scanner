ARG BUILD_FROM
FROM $BUILD_FROM
# Alpine packages - no compilation needed on the Home Assistant host
RUN apk add --no-cache python3 py3-requests py3-beautifulsoup4 py3-lxml tzdata
COPY scanner.py /scanner.py
COPY run.sh /run.sh
RUN chmod a+x /run.sh
CMD [ "/run.sh" ]
