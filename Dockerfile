FROM python:3.12-slim

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY main.py .
COPY templates/ templates/
COPY static/ static/

# Create a data directory for the persistent SQLite volume
RUN mkdir -p /data

# Default environment configurations for Docker container
ENV DB_PATH=/data/temp_mail.db
ENV SMTP_PORT=25
ENV SMTP_HOST=0.0.0.0
ENV API_PORT=5000

# Expose HTTP web interface and SMTP port
EXPOSE 5000
EXPOSE 25

# Command to run the application
CMD ["python", "main.py"]
