# Multi-stage build
FROM python:3.11-slim AS builder
WORKDIR /app

# Install build dependencies in a single layer
RUN apt-get update && apt-get install -y \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*
    
# Copy requirements and install Python dependencies
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Production stage
FROM python:3.11-slim AS production

WORKDIR /app
# Install only runtime dependencies if needed
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    && apt-get clean && \
    rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

# Copy Python dependencies from builder stage
COPY --from=builder /opt/venv /opt/venv

# Copy application code
COPY mail2cal.py .

# Set env
ENV PATH="/opt/venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Use labels for metadata
LABEL maintainer="asyafiqe" \
      version="0.1" \
      description="Email Calendar Automator"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import sys; sys.exit(0)" || exit 1
    
CMD ["python", "mail2cal.py"]

