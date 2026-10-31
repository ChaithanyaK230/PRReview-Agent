# One image, two Fly process groups: api (uvicorn) and worker (arq). See fly.toml.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1

WORKDIR /app
# Dependencies first so code changes don't reinstall them
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

# Never run as root: the app only needs to read its own files
RUN useradd --create-home app && chown -R app /app
USER app

EXPOSE 8080
CMD ["sh", "scripts/start.sh"]
