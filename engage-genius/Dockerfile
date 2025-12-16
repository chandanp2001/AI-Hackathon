# Use the specified base image
FROM c.rzp.io/razorpay/rzp-docker-image-inventory-multi-arch:rzp-golden-image-base-python-3.12-alpine3.20 as builder

WORKDIR /app

# Install build dependencies
RUN apk add --update --no-cache \
    gcc \
    musl-dev \
    python3-dev \
    libffi-dev \
    openssl-dev \
    make \
    tzdata \
    libxml2-dev \
    libxslt-dev \
    jpeg-dev \
    zlib-dev \
    && update-ca-certificates 2>/dev/null || true

# Copy requirements first to leverage Docker cache
COPY requirements.txt .

# Install dependencies with pre-built wheels for larger packages
RUN pip install --no-cache-dir numpy==1.26.4 pandas==2.1.4 --only-binary=numpy,pandas && \
    pip install --no-cache-dir -r requirements.txt

# Download NLTK data
RUN python -m nltk.downloader punkt stopwords -d /usr/local/share/nltk_data

# Final stage - use the same base image
FROM builder as final

WORKDIR /app

# Install runtime dependencies
RUN apk add --update --no-cache \
    bash \
    git \
    ca-certificates \
    libxml2 \
    libxslt \
    jpeg \
    tzdata \
    && update-ca-certificates 2>/dev/null || true

# Copy application code
COPY . .

# Create necessary directories
RUN mkdir -p /app/credentials /app/logs /app/cache

# Setup permissions for directories
RUN chmod -R 777 /app/credentials /app/logs /app/cache

# Create volume mount points for credentials, logs, and cache
VOLUME ["/app/credentials", "/app/logs", "/app/cache"]

# Environment variables with proper credential paths and SSL configuration
ENV PYTHONUNBUFFERED=1 \
    PORT=8000 \
    GOOGLE_APPLICATION_CREDENTIALS=/app/credentials/credentials.json \
    GOOGLE_CREDENTIALS_PATH=/app/credentials/credentials.json \
    GOOGLE_TOKEN_PATH=/app/credentials/token.json \
    NLTK_DATA=/usr/local/share/nltk_data \
    PYTHONHTTPSVERIFY=0 \
    TZ=Asia/Kolkata

# Make sure SSL certificates are accessible
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

# Set up health checks
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD wget -O - http://localhost:8000/health > /dev/null || exit 1

# Expose the port the app runs on
EXPOSE 8000

# Command to run the application with proper port binding
CMD ["gunicorn", "app:app", "--workers=4", "--worker-class=gevent", "--bind=0.0.0.0:8000", "--log-level=info", "--timeout=120"]