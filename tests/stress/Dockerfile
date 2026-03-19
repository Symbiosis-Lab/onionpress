# Stress test worker/poll client — extends OnionPress tor image.
# Pre-installed: python3, curl, tor, netcat-openbsd, xxd, stress scripts.
# No apt-get needed at runtime.

FROM ghcr.io/brewsterkahle/onionpress-tor:latest

# Install python3 (for verify-worker.py on poll clients and worker-bootstrap.py on workers)
RUN apt-get update -qq && apt-get install -y -qq python3-minimal >/dev/null 2>&1 && rm -rf /var/lib/apt/lists/*

COPY worker-server.py /worker-server.py
COPY worker-bootstrap.py /worker-bootstrap.py
COPY verify-worker.py /verify-worker.py
COPY tor-watchdog.sh /tor-watchdog.sh
COPY onion_auth.py /onion_auth.py

RUN chmod +x /tor-watchdog.sh

ENTRYPOINT ["sh", "-c", "sleep infinity"]
