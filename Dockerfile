FROM python:3.11-slim

WORKDIR /app

# Install system dependencies including ffmpeg for video/audio processing
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

ENV PORT=10000
CMD ["python", "server.py", "--silent"]
