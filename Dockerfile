FROM python:3.12-alpine

LABEL org.opencontainers.image.title="coolify-updates-manager" \
      org.opencontainers.image.description="Docker image update dashboard with one-click deploy on Coolify" \
      org.opencontainers.image.source="https://github.com/rafaelkillua/coolify-updates-manager" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UPDATES_STATE=/data/state.json

WORKDIR /app
COPY app.py /app/app.py

RUN mkdir -p /data

EXPOSE 8799

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8799/healthz', timeout=4).status == 200 else 1)"

CMD ["python", "/app/app.py"]
