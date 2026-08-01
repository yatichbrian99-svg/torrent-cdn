FROM python:3.11-slim

WORKDIR /app

# Install system dependencies (curl + ffmpeg for video thumbnail generation)
RUN apt-get update && apt-get install -y curl ffmpeg && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --upgrade pip && pip install --no-cache-dir --default-timeout=120 -r requirements.txt

# Copy source code
COPY . .

EXPOSE 8000

# Production: use gunicorn with uvicorn workers for better concurrency
# Render sets the PORT env var automatically
CMD ["sh", "-c", "gunicorn app.main:app --workers 2 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --timeout 120 --keep-alive 5"]
