# One image for all four services; SERVICE picks which one runs (gateway | resizer | classifier | store).
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE=gateway \
    PORT=8000 \
    MODEL_PATH=/models/mobilenetv2-12.onnx \
    LABELS_PATH=/models/imagenet_classes.txt

WORKDIR /app

COPY requirements-core.txt requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY scripts/download_model.py scripts/
RUN python scripts/download_model.py /models

COPY imgsvc ./imgsvc

RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=20s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/health', timeout=2)"

CMD ["sh", "-c", "exec uvicorn imgsvc.${SERVICE}:app --host 0.0.0.0 --port ${PORT} --no-access-log"]
