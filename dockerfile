# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm as builder

# System deps
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libharfbuzz0b \
    libfribidi0 \
    fonts-dejavu \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Install Python dependencies separately for better caching
COPY requirements.txt .
RUN pip install --user --no-cache-dir -r requirements.txt

# Final stage
FROM python:3.11-slim-bookworm

# Install runtime system dependencies only (no build tools)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libharfbuzz0b \
    libfribidi0 \
    fonts-dejavu \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Copy installed Python packages from builder
COPY --from=builder /root/.local /root/.local
ENV PATH=/root/.local/bin:$PATH
ENV PYTHONPATH=/app

# Prevent Python from writing .pyc files + ensure unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Workdir
WORKDIR /app

# Copy application code
COPY . .

# Streamlit defaults
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
EXPOSE 8501

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Run app
CMD ["python", "-m", "streamlit", "run", "app/main2.py", "--server.port=8501", "--server.address=0.0.0.0"]