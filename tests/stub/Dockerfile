# A stub agent image for the test bench.
#
# It answers the health probe the StatefulSet points at, on the same loopback
# endpoint the real image uses, so pods become Ready without Hermes, the AI
# Gateway or a real homeserver. It also tolerates the seed init container's
# shape: the same /bin/sh, cp and touch the real image has.

FROM alpine:3.20

# curl answers the health probe, exactly as the pilot's probes do. httpd is
# the busybox-extras web server that serves it.
RUN apk add --no-cache curl busybox-extras

# busybox httpd answers 200 on /health.
RUN mkdir -p /www && printf 'ok\n' > /www/health

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8642
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
