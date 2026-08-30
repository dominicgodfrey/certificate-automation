FROM python:3.12-slim

# Install system dependencies required by Playwright/Chromium
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    libatspi2.0-0 \
    libwayland-client0 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Chromium for Playwright
RUN playwright install chromium

# Copy project files
COPY . .

# Create data directory for SQLite fallback
RUN mkdir -p data output

# Expose port
EXPOSE 10000

# 1 worker: in-memory job/preset state assumes a single process, and a
# second Chromium during concurrent sends can OOM the instance.
# 600s timeout: /download-all renders every PDF in one request.
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--timeout", "600", \
     "--access-logfile", "-"]
