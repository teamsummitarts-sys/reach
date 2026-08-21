# REACH — standalone container. Nothing outside reach-app/ is copied in.
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py ./
COPY reach ./reach
COPY templates ./templates
COPY static ./static

# The SQLite store belongs on a mounted volume; the default path here is inside
# the image and resets with the container, which is fine for a trial run and
# wrong for real use.
ENV REACH_DB_PATH=/data/reach.db
VOLUME ["/data"]

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz')"

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--timeout", "120", "app:app"]
