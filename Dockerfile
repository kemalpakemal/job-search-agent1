FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY source.zip /tmp/source.zip
RUN python -c "import zipfile; zipfile.ZipFile('/tmp/source.zip').extractall('/app')" \
    && pip install --no-cache-dir -r /app/requirements.txt \
    && mkdir -p /data
COPY scoring.py /app/scoring.py
EXPOSE 8000
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1"]
