FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home checkin && mkdir /data && chown checkin /data
COPY app.py control.py ./
COPY templates ./templates
ENV PYTHONUNBUFFERED=1 DATABASE=/data/checkin.db
USER checkin
EXPOSE 8888
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8888/healthz', timeout=3)"
CMD ["python", "app.py"]
