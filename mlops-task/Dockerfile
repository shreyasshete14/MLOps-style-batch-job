# Dockerfile — mlops-task
FROM python:3.9-slim

# Prevent .pyc files and enable unbuffered stdout (so logs stream live)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first (better layer caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code + data + config into the image
COPY run.py .
COPY config.yaml .
COPY data.csv .

# No hard-coded paths: entrypoint passes explicit CLI args.
# Output files land in /app inside the container (metrics.json, run.log).
ENTRYPOINT ["python", "run.py", "--input", "data.csv", "--config", "config.yaml", \
            "--output", "metrics.json", "--log-file", "run.log"]
