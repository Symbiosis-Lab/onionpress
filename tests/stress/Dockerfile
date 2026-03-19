# Stress test worker — extends the OnionPress tor image with stress scripts.
# No apt-get needed at runtime — everything is pre-installed in the base image
# (python3, curl, tor, netcat-openbsd, xxd, onion_auth.py).
#
# Build & push:
#   docker build -t ghcr.io/brewsterkahle/onionpress-stress-worker:latest tests/stress/
#   docker push ghcr.io/brewsterkahle/onionpress-stress-worker:latest

FROM ghcr.io/brewsterkahle/onionpress-tor:latest

COPY worker-server.py /worker-server.py
COPY worker-bootstrap.py /worker-bootstrap.py
COPY verify-worker.py /verify-worker.py
COPY tor-watchdog.sh /tor-watchdog.sh

RUN chmod +x /tor-watchdog.sh

ENTRYPOINT ["sh", "-c", "sleep infinity"]
