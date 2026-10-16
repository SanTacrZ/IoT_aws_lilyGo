FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py scheduler.py ./
EXPOSE 5050
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:5050/api/agente/health', timeout=4).status==200 else 1)"
CMD ["gunicorn", "--bind", "0.0.0.0:5050", "--workers", "2", "--timeout", "90", "app:app"]