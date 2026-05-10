# python:3.12-slim — small image, fast Cloud Run cold starts
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App entrypoint and modules (repo root — not under app/)
COPY main.py crawler.py classifier.py ./

ENV PORT=8080
EXPOSE 8080

# Cloud Run sets PORT; shell form expands ${PORT}
CMD ["/bin/sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8080}"]
