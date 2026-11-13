FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY source.b64.* /tmp/
RUN set -e; cat /tmp/source.b64.* | base64 -d > /tmp/source.zip; \
    python -c "import zipfile; z=zipfile.ZipFile('/tmp/source.zip'); bad=z.testzip(); assert bad is None, bad; z.extractall('/app')"; \
    pip install --no-cache-dir -r /app/requirements.txt; \
    mkdir -p /data
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
