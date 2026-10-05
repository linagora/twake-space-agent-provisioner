#!/bin/sh
# Serve the health endpoint on the port the StatefulSet probes.
#
# The real image runs Hermes' gateway; this stub only needs to answer the
# probe, so a pod becomes Ready in seconds without Hermes or a homeserver.
exec httpd -f -p 8642 -h /www
