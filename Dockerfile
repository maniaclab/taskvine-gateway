# Bakes the server and its dependencies into the image at build time
# instead of installing them (via pixi, from a pinned git rev) on every
# pod start - same reasoning as worker/Dockerfile, just for the gateway
# itself rather than the workers it creates. Unlike ndcctools (worker/),
# every server dependency is plain PyPI, so no conda/pixi is needed here.
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir ".[server]"

EXPOSE 8080
ENTRYPOINT ["taskvine-gateway"]
