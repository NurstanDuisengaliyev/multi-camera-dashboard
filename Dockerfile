# Multi-camera monitoring dashboard: one container with Flask + camera workers + YOLO.
FROM python:3.12-slim

# No .pyc files; print logs immediately (important for `docker logs`).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System libraries OpenCV needs.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 1) PyTorch CPU-only build. The default build includes NVIDIA GPU libraries
#    (several GB) that a normal cloud VM without a GPU can't use.
RUN pip install --no-cache-dir torch==2.8.0 torchvision==0.23.0 \
      --index-url https://download.pytorch.org/whl/cpu

# 2) The rest of the Python packages. Copied separately so Docker can reuse this
#    layer when only our code changes (much faster rebuilds).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3) Download the YOLO weights at build time, so the container doesn't need
#    internet access when it starts.
RUN python -c "from ultralytics import YOLO; YOLO('yolov8n.pt')"

# 4) Our code, configuration and videos.
COPY app ./app
COPY scripts ./scripts
COPY videos ./videos
COPY config.yaml .

# 8000 = HTTP dashboard, 8443 = HTTPS (needed by the phone camera page /push)
EXPOSE 8000 8443

# One gunicorn worker process (all camera threads live inside it), many threads
# because every open video stream in a browser keeps one thread busy.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "32", \
     "--timeout", "120", "--access-logfile", "-", "app.main:create_app()"]
