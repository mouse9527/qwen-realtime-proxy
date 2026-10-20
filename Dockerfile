# python:3.13-slim, pinned by index digest (multi-arch); bump deliberately.
FROM python:3.13-slim@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

# Non-root; nothing is written at runtime, so a read-only root filesystem works.
USER 1000:1000
EXPOSE 8080
CMD ["python", "-m", "app.proxy"]
