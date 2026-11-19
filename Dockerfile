# Multi-stage / clean python runtime for Tapo Charging Bot
FROM python:3.12-slim

# Prevent Python from buffering stdout/stderr and writing bytecode
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Europe/Madrid

WORKDIR /app

# Install tzdata for accurate local timestamps
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY src/ ./src/

# Create persistent data directory for SQLite database
RUN mkdir -p /app/data

# Default environment configuration
ENV DATABASE_PATH=/app/data/tapo_charging.db

# Run bot
CMD ["python", "-m", "src.main"]
