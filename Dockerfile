# Who's That Pokemon? - video creation service
#
# Build & deploy (from this directory):
#   gcloud run deploy whos-that-pokemon-video \
#     --source . \
#     --region us-central1 \
#     --allow-unauthenticated \
#     --concurrency 1 \
#     --memory 1Gi \
#     --cpu 1 \
#     --timeout 180 \
#     --max-instances 3 \
#     --set-env-vars ALLOWED_ORIGINS=https://yourname.github.io
#
# --concurrency 1 matters: each request pins the CPU for a real ffmpeg
# encode, so one request per container instance keeps things fast and
# avoids instances fighting each other over the same core.

FROM python:3.12-slim

# ffmpeg is the only non-Python dependency this service needs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY Mango ./Mango

ENV PORT=8080
EXPOSE 8080

# gunicorn: one worker (matches --concurrency 1 above), generous timeout
# for the ffmpeg call itself.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 1 --timeout 170 main:app"]
