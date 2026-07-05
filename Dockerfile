FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1
RUN pip install --no-cache-dir uv
WORKDIR /app

# Install dependencies first (separate layer, cached across code-only changes)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Then install the project itself
COPY . .
RUN uv sync --frozen --no-dev

# Without this, every `uv run` at container start (and every `docker exec ...
# uv run tiro ...`) re-syncs the venv against the *default* dependency group
# — which includes dev tools (pytest, ruff) — undoing the --no-dev sync
# above, triggering network installs, and rebuilding the project package on
# every invocation. UV_NO_SYNC pins runtime `uv run` to the venv exactly as
# built, with no implicit re-sync.
ENV UV_NO_SYNC=1

# TIRO_LIBRARY_PATH overlays library_path via the TIRO_* env config overlay
# (tiro/config.py); TIRO_HOST overlays host to 0.0.0.0 for container access.
# Binding to a non-loopback host REQUIRES a password (Phase 0 invariant,
# unchanged here) — see docker-compose.yml / README "Docker" section for the
# first-run password setup flow.
#
# --config /data/config.yaml (rather than the default ./config.yaml under
# /app) keeps config.yaml — which is where `tiro set-password` writes the
# bcrypt hash — inside the /data volume alongside the library, so it
# survives container recreation (not just restarts). Without this, a
# password set via `docker compose run --rm ...` would land in that
# throwaway container's own writable layer and vanish with --rm.
ENV TIRO_LIBRARY_PATH=/data TIRO_HOST=0.0.0.0
VOLUME /data
EXPOSE 8000
CMD ["uv", "run", "tiro", "--config", "/data/config.yaml", "run", "--no-browser"]
