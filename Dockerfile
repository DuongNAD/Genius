FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source
COPY . .

# Expose ports: Hub (8000), Grok (8001), Claude (8002), Codex (8003), Tester (8004), Security (8005), DevOps (8006), Dashboard (8080)
EXPOSE 8000 8001 8002 8003 8004 8005 8006 8080

# Default command to run serve.py
CMD ["python", "serve.py"]
