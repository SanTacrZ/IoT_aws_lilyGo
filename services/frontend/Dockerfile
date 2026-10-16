FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 80
# access-logfile - : sin esto no se ve que peticiones llegan de verdad
# (clave para diagnosticar "se queda cargando").
CMD ["gunicorn", "--bind", "0.0.0.0:80", "--workers", "2", "--timeout", "120", "--access-logfile", "-", "app:server"]
