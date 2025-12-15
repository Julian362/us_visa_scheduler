# App container (Python only). Use with selenium/standalone-chrome via docker-compose.
FROM python:3.12-slim

WORKDIR /app

# Install system deps for requests/openssl
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default command
CMD ["python", "visa.py"]
