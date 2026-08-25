# Bakes ndcctools (vine_worker) into the image at build time instead of
# resolving/downloading it from conda-forge on every pod start - that was
# the actual cause of slow worker cold-starts, not anything about the pool
# size or scheduling itself. See taskvine-gateway's k8s.py for how these
# get scheduled.
FROM ghcr.io/prefix-dev/pixi:0.76.1

WORKDIR /app
COPY pixi.toml .
RUN pixi install --manifest-path /app/pixi.toml

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
