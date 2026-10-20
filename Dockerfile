# Google Cloud Run source for StockPilot AI.
# Canonical UI: this folder's mobile.html (parent ../mobile.html is a symlink).
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose port (Cloud Run defaults to 8080)
EXPOSE 8080

# Run FastAPI via Uvicorn
CMD exec uvicorn app:app --host 0.0.0.0 --port ${PORT}
