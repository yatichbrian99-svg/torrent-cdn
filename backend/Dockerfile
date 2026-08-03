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

# Production: single gunicorn worker with the async uvicorn worker class.
# uvicorn is fully async, so ONE worker already handles concurrent requests —
# and the app keeps chunked-upload sessions / rate-limit / analytics state in
# PROCESS-LOCAL dicts, so multiple workers would split sessions across
# processes and 404 chunk uploads (session created on worker A, chunk served
# by worker B). Single instance (Render free) = single process = consistent
# in-memory state; Redis backs the presign sessions for instance-recycle
# durability. Render sets the PORT env var automatically.
CMD ["sh", "-c", "gunicorn app.main:app --workers 1 --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --timeout 120 --keep-alive 5"]
