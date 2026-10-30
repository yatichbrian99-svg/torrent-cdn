# Patch-pinned (python:3.11.9-slim, not the mutable python:3.11-slim) so a
# rebuilt image is byte-reproducible and can't silently drift to a newer base.
# Bump deliberately when a security release lands.
FROM python:3.11.9-slim

WORKDIR /app

# Install system dependencies (curl + ffmpeg for video thumbnail generation).
#
# The base tag is pinned, but a pinned base ages: 3.11.9-slim's bookworm
# snapshot carried 67 fixed HIGH/CRITICAL CVEs by 2026-10 (gpgv, libexpat,
# gnutls, krb5, libcap…), which is what the blocking Trivy gate reported. An
# explicit upgrade applies the security patches Debian has already published,
# so the pin still fixes the Python version while the OS packages stay current.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends curl ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt constraints.txt ./
# pip/setuptools/wheel come from the base image and age with it (the blocking
# scan flagged wheel 0.44.0); upgrade the toolchain before installing anything.
# constraints.txt carries the security floors for transitive packages the scan
# flagged — a fresh resolve is not a fix if the index still offers the old one.
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir --default-timeout=120 \
        -c constraints.txt -r requirements.txt

# Drop the installer from the runtime image. The service installs nothing at
# run time, so pip is dead weight — and it is what the blocking Trivy gate kept
# reporting: pip ships vendored copies of msgpack, urllib3 and setuptools
# (declared in pip/_vendor/vendor.txt) that the scan reads as installed
# packages, five HIGH findings in code this container never executes. Removing
# pip removes the findings at their source rather than suppressing them, and it
# takes a download-a-payload tool away from anyone who lands in the container.
# Keep setuptools: some libraries still import `pkg_resources` at import time.
RUN rm -rf /usr/local/lib/python3.11/site-packages/pip \
        /usr/local/lib/python3.11/site-packages/pip-*.dist-info \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.11

# Copy source code
COPY . .

# Run as a non-root user (least privilege): a container compromise must not
# hand the attacker root in the image. The app writes only to /tmp (chunked
# upload assembly, ffmpeg thumbnails) and binds a high port, so non-root is
# sufficient. Render's health check and the docker-compose /tmp volume both
# work unprivileged (the /tmp volume inherits the image's ownership).
RUN useradd --create-home --shell /bin/false appuser \
    && mkdir -p /tmp \
    && chown -R appuser:appuser /app /tmp

USER appuser

EXPOSE 8000

# Production: gunicorn with the async uvicorn worker class. uvicorn is fully
# async, so ONE worker already handles many concurrent requests — the default
# (and the right count on Render's free tier, where 0.1 CPU is the hard
# ceiling). On a BIGGER plan (more CPU/RAM), raise WEB_CONCURRENCY to add
# workers: chunked-upload AND presign sessions are Redis-backed, so a session
# created on one worker is continued on another (the in-memory dicts are only
# a Redis-down fallback). Keep workers × DB_POOL_MAX_SIZE under the Supabase
# pooler's connection cap (free tier ~60). Render sets the PORT env var
# automatically.
# `--config gunicorn_conf.py` replaces gunicorn's `Server: gunicorn/<version>`
# header with a version-free product name (the app does the same for uvicorn's
# header). See backend/gunicorn_conf.py.
CMD ["sh", "-c", "gunicorn app.main:app --config gunicorn_conf.py --workers ${WEB_CONCURRENCY:-1} --worker-class uvicorn.workers.UvicornWorker --bind 0.0.0.0:${PORT:-8000} --timeout 120 --keep-alive 5"]
