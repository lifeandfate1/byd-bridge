FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Install adb from Debian repos (simplest) + certs
RUN apt-get update && apt-get install -y --no-install-recommends \
      adb ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python deps
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# App files
COPY byd_mqtt_bridge.py /app/
COPY entrypoint.sh /app/
RUN chmod +x /app/entrypoint.sh

CMD ["/app/entrypoint.sh"]
