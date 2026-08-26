# Multi-stage: Node builds the React bundle, Python serves it alongside the API.
# The frontend needs no runtime of its own, which is why this is one Cloud Run service.
# Build context is the repository root.

FROM node:22-alpine AS frontend
WORKDIR /build

# Vite inlines `import.meta.env.VITE_*` at build time, so these must be present *here* rather
# than at deploy. Supplying them as runtime environment variables does nothing: the strings are
# already compiled into the bundle by then.
#
# Getting this wrong does not fail the build. It produces a working API, a working assistant,
# and a blank grey rectangle where the map should be — and `.env.local` is gitignored, so the
# values are not in the build context to fall back on.
ARG VITE_GOOGLE_MAPS_BROWSER_KEY
ARG VITE_GOOGLE_MAPS_MAP_ID
ENV VITE_GOOGLE_MAPS_BROWSER_KEY=${VITE_GOOGLE_MAPS_BROWSER_KEY} \
    VITE_GOOGLE_MAPS_MAP_ID=${VITE_GOOGLE_MAPS_MAP_ID}

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./

# `npm run build` is `tsc --noEmit && vite build`, and two test files import the generated
# contract as `../../../shared/openapi.json` — which resolves outside the frontend directory.
# Copied to `/shared` so that path lands, keeping the image's typecheck identical to the one
# `make check` runs. The alternative, having the image skip typechecking tests, would let the
# two diverge, and a build that verifies something other than what CI verifies is worse than
# an odd-looking COPY.
COPY shared/ /shared/

# Refuse to ship a bundle with no map. A build arg that never arrived is silent otherwise, and
# the symptom appears only in a browser, only after a deploy.
RUN test -n "$VITE_GOOGLE_MAPS_BROWSER_KEY" \
      || (echo "VITE_GOOGLE_MAPS_BROWSER_KEY is empty — the map would not render" >&2; exit 1)
RUN npm run build


FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_DEV=1 \
    PYTHONUNBUFFERED=1 \
    MOTOROOTER_STATIC_DIR=/app/static

# Dependencies first, in their own layer, so source edits do not re-resolve them.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-install-project

COPY backend/src ./src
RUN uv sync --frozen

COPY --from=frontend /build/dist ./static

# Cloud Run injects PORT and expects the container to listen on it.
ENV PORT=8080
EXPOSE 8080
CMD ["sh", "-c", "uv run uvicorn motorooter.app:create_app --factory --host 0.0.0.0 --port ${PORT}"]
