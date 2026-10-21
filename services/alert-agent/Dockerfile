FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./
# Sin root: el servicio solo lee la BD y habla con las APIs de mensajeria.
RUN useradd --system --no-create-home alertas
USER alertas
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,time,sys; sys.exit(0 if time.time()-os.path.getmtime('/tmp/alertas-heartbeat') < 180 else 1)"
CMD ["python", "main.py"]
