#!/bin/sh
# start.sh - AP Real Estate Automation System startup script

echo "Starting AP Real Estate Automation System..."

# Create required runtime directories
mkdir -p exports/iif
mkdir -p downloads/envelopes
mkdir -p keys

# Start FastAPI server
exec uvicorn api.main:app \
    --host 0.0.0.0 \
    --port ${PORT:-8000} \
    --workers 1 \
    --log-level info
