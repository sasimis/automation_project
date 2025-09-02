# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

# Prevent Python from writing .pyc files + ensure unbuffered logs
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install only essential system dependencies for WeasyPrint
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    libcairo2 \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libglib2.0-0 \
    fonts-dejavu \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Copy only requirements first for better Docker layer caching
COPY requirements.txt /app/requirements.txt

# Install Python deps
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# Copy the rest of the project
COPY . /app

# Streamlit defaults
ENV STREAMLIT_BROWSER_GATHER_USAGE_STATS=false
EXPOSE 8501

# Run app
CMD ["python", "-m", "streamlit", "run", "app/main2.py", "--server.port=8501", "--server.address=0.0.0.0"]