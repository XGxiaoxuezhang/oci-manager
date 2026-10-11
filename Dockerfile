FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    OCI_MANAGER_HOST=0.0.0.0 \
    OCI_MANAGER_PORT=5080 \
    OCI_MANAGER_DATA_DIR=/data \
    OCI_MANAGER_DEBUG=0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY oci-manager ./oci-manager

RUN mkdir -p /data/tenants
VOLUME ["/data"]

EXPOSE 5080

# 应用自带 /healthz；slim 镜像没有 curl，用 python 一行式探测
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5080/healthz', timeout=4)"]

CMD ["python", "oci-manager/app.py"]
