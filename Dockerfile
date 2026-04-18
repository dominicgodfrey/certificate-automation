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

# Run with Gunicorn
# - 1 worker: each send job launches Chromium in a background thread;
#   with 2 workers, two simultaneous sends can OOM the instance. A
#   single staff user polling progress is fine with 1 worker.
# - 600s timeout: /download-all renders every PDF synchronously in the
#   HTTP request and can easily exceed the old 120s ceiling at 400+
#   students. 600s gives ~1.5s per PDF at 400 students, with headroom.
# - bind to 0.0.0.0:10000 (Render's expected port)
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "1", \
     "--timeout", "600", \
     "--access-logfile", "-"]
