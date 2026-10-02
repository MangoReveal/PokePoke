# Who's That Pokemon? - video creation service
#
# Deploys as a Docker web service (tested on Render's free tier - just
# connect the repo, pick "Docker" as the environment, free instance type).
# Works on Cloud Run or anywhere else that runs a Dockerfile too.

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

# One worker (free-tier RAM is tight, a second worker process would
# double the memory footprint), but 2 threads so a quick health-check
# ping can still get answered while a slow ffmpeg encode is running on
# the other thread - subprocess.run() releases the GIL while it waits,
# so this actually works. Without this, the service looks "down" to
# anything else hitting it for the whole length of every video creation.
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:${PORT} --workers 1 --threads 2 --timeout 170 main:app"]
