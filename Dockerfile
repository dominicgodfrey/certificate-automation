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
# - 2 workers (Render free tier has limited memory)
# - 120s timeout (certificate rendering can take time)
# - bind to 0.0.0.0:10000 (Render's expected port)
CMD ["gunicorn", "wsgi:app", \
     "--bind", "0.0.0.0:10000", \
     "--workers", "2", \
     "--timeout", "120", \
     "--access-logfile", "-"]
