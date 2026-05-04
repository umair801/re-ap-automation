# Dockerfile
# AP-AI: Enterprise Accounts Payable Automation Agent
# Datawebify — ap.datawebify.com

FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies for PyMuPDF, pytesseract, and OCR
RUN apt-get update --fix-missing && apt-get install -y --fix-missing \
    tesseract-ocr \
    tesseract-ocr-eng \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    gcc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create runtime folders for docx batch processing
RUN mkdir -p /app/docx_input /app/docx_output /app/tmp_uploads /app/tmp_exports

# Copy and make start script executable (must be done as root)
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

# Create non-root user for security
RUN adduser --disabled-password --gecos "" appuser \
    && chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD python -c "import httpx; httpx.get('http://localhost:8000/metrics/health')" || exit 1

# Start the FastAPI app via shell script
CMD ["/bin/sh", "/app/start.sh"]
