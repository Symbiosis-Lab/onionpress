# Stress test worker/poll client — extends OnionPress tor image.
# Pre-installed: python3, curl, tor, netcat-openbsd, xxd, stress scripts.
# Base image already has python3 — no apt-get needed.

FROM ghcr.io/brewsterkahle/onionpress-tor:latest

COPY worker-server.py /worker-server.py
COPY worker-bootstrap.py /worker-bootstrap.py
COPY verify-worker.py /verify-worker.py
COPY tor-watchdog.py /tor-watchdog.py
COPY onion_auth.py /onion_auth.py

ENTRYPOINT ["sh", "-c", "sleep infinity"]
