FROM python:3.13-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 AUTOSYNC_DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1000 autosync && useradd --uid 1000 --gid 1000 --no-create-home autosync \
    && mkdir /data && chown 1000:1000 /data
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bridge ./bridge
COPY run.py ./
USER 1000:1000
EXPOSE 8095
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8095/healthz', timeout=3).read()"
ENTRYPOINT ["python", "run.py"]
CMD ["serve"]
