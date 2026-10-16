FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
EXPOSE 5000
# 1 worker (no 2): el rate limit vive en memoria del proceso, con varios workers
# el limite efectivo se multiplica y deja pasar el triple de lo que se cree.
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "120", "app:app"]
