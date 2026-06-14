FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
WORKDIR /app

# Install minimal build deps (kept small for slim images)
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY backend/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy source
COPY backend /app

EXPOSE 8000

# Run the FastAPI app via Uvicorn
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
