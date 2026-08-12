FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN mkdir -p /app/data

ENV PYTHONUNBUFFERED=1
ENV PORT=8080
ENV RUN_WORKER=1
ENV DATABASE_PATH=/app/data/revenue_agent.sqlite3

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=2).read()"]
CMD ["python3", "main.py"]
