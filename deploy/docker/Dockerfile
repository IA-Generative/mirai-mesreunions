FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (ffmpeg for transcode worker)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Copy shared library
COPY libs/shared /app/libs/shared
# Copy service code for Kubernetes/runtime images
COPY services /app/services

# Default: overridden per service
CMD ["python", "-m", "app.main"]
