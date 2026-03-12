# Use Python 3.11 as base image (3.14 might have compatibility issues)
FROM python:3.11-slim

# Set metadata
LABEL maintainer="ketharinath14@gmail.com"
LABEL description="NJ Transit Delay Alerts - Backend Service"

# Disable Python output buffering so logs appear immediately in Azure
ENV PYTHONUNBUFFERED=1

# Set working directory inside container
WORKDIR /app

# Copy requirements file first (for better caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all application code
COPY app.py .
COPY worker.py .
COPY database.py .
COPY njtransit.py .
COPY notifications.py .
COPY cache.py .
COPY gtfs.py .
COPY lirr_gtfs.py .
COPY lirr_realtime.py .

# Expose port 8000 for the API
EXPOSE 8000

# Create volume for database persistence
VOLUME ["/app/data"]

# Run both API server and worker
# In production, you'd run these in separate containers
# For learning/demo, we run both together
CMD python app.py & python worker.py
