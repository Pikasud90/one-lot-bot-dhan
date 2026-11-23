# One Lot Desk (signal-only: it places no orders). Build: docker build -t onelot-desk .
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

# Dependencies first, from the lock file, so this layer is cached across code changes.
COPY requirements.txt requirements.lock.txt ./
RUN pip install -r requirements.lock.txt

COPY config ./config
COPY src ./src

# A non-root user owns the folders the app writes to (data and logs are volumes in compose).
RUN useradd --create-home --uid 1000 onelot \
    && mkdir -p /app/data /app/logs \
    && chown -R onelot:onelot /app/data /app/logs \
    && chown onelot:onelot /app
USER onelot

EXPOSE 8787

# The app listens on all interfaces INSIDE the container; docker-compose.yml publishes the
# port on 127.0.0.1 of the host only.
CMD ["python", "-m", "onelot", "run", "--host", "0.0.0.0", "--port", "8787"]
