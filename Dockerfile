# syntax=docker/dockerfile:1.7

# ============================================================================
# Builder stage: Install dependencies with uv
# ============================================================================
FROM python:3.12-slim AS builder

# Install uv (pinned version for reproducibility)
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /uvx /bin/

# Configure uv for Docker builds
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

WORKDIR /app

# Install dependencies first (cached layer)
# Copy only dependency files to leverage Docker cache
COPY pyproject.toml uv.lock* ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev || \
    uv sync --no-install-project --no-dev

# Copy application source and install project
COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev || \
    uv sync --no-dev

# ============================================================================
# Runtime stage: Minimal production image
# ============================================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Create non-root user for security
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid 1000 --shell /bin/bash appuser

# Copy only the virtual environment from builder (not uv itself)
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv

# Copy application source
COPY --chown=appuser:appuser src/ ./src/

# Add venv to PATH
ENV PATH="/app/.venv/bin:$PATH"

# Switch to non-root user
USER appuser

# Expose port
EXPOSE 8080

# Run with uvicorn
CMD ["uvicorn", "src.beerbot.main:app", "--host", "0.0.0.0", "--port", "8080"]
