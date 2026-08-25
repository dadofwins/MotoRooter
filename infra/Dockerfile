# Multi-stage: Node builds the React bundle, Python serves it alongside the API.
# The frontend needs no runtime of its own, which is why this is one Cloud Run service.
# Build context is the repository root.

FROM node:22-alpine AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
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
