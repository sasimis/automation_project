# syntax=docker/dockerfile:1
FROM python:3.11-slim

# Prevent Python from writing .pyc files + ensure unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# System deps (WeasyPrint & friends: cairo, pango, gdk-pixbuf, harfbuzz, fribidi, glib, fonts)
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    build-essential \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    libharfbuzz0b \
    libfribidi0 \
    libffi-dev \
    libjpeg62-turbo \
    libpng16-16 \
    shared-mime-info \
    fonts-dejavu \
    fonts-liberation \
    curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Copy only requirements first for better Docker layer caching
COPY requirements.txt /app/requirements.txt

# Install Python deps (keep versions from your file)
RUN python -m pip install --upgrade pip setuptools wheel \
 && pip install -r /app/requirements.txt

# Copy the rest of the project
COPY . /app

# Streamlit defaults (reduce noisy telemetry, set browser server address)
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
EXPOSE 8501

# Healthcheck (optional)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -f http://localhost:8501/_stcore/health || exit 1

# Run app
CMD ["python", "-m", "streamlit", "run", "app/main2.py", "--server.port=8501", "--server.address=0.0.0.0"]
