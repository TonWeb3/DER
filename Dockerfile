FROM python:3.11-slim

# Prevent python from buffering stdout/stderr and writing .pyc files
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8070

WORKDIR /app

# Install curl for health checking
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Expose default port
EXPOSE 8070

# Launch Uvicorn with dynamic Railway PORT, proxy headers, and websockets protocol
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-8070} --proxy-headers --forwarded-allow-ips '*' --ws websockets"]
