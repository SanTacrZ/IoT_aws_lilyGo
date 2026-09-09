# Política de seguridad — IoT AWS LilyGo

> Objetivo: **confidencialidad, integridad y autenticidad** de los datos desde el
> sensor LilyGo hasta su escritura en la nube (RDS + S3). Nada viaja ni se guarda en plano.

## 1. Modelo de amenazas

| Amenaza | Control actual | Estado |
|---|---|---|
| Suplantación del dispositivo (POST falso) | API-Key + HMAC-SHA256 sobre `timestamp.body`, `compare_digest` | ✅ Activo |
| Reenvío de tramas capturadas (replay) | `X-Timestamp` con ventana `MAX_SKEW_S=300s` + NTP en placa | ✅ Activo |
| Lectura de credenciales en código/repo | Secretos solo en `.env` del EC2 y Secrets Manager; repo solo con `.env.example` y placeholders | ✅ Activo |
| Acceso a la DB con clave quemada | Backend lee credenciales vía **boto3 → Secrets Manager** (`DB_SECRET_ARN`), con fallback a env | ✅ Activo |
| Pérdida/corrupción del dato caliente | Doble escritura: **RDS (fuente primaria) + S3 JSONL (respaldo frío)** con `SSE AES256`; el backup nunca tumba el ingest | ✅ Activo |
| Escucha en red (sniffing) | **TLS pendiente**: hoy HTTP en `:8000` (red de prueba). Ver §4 | ⚠️ Riesgo aceptado solo en POC |
| Robo de la API-Key/HMAC | Rotación manual documentada (§3); sin TLS un atacante en la red podría verlos | ⚠️ Ver §4 |
| Borrado/daño del RDS | Sin `DeletionProtection`, sin backups, sin cifrado en reposo | ⚠️ Endurecer antes de prod (§4) |

## 2. Cadena de integridad LilyGo → nube

1. **Placa** (`lilygo_secure_post.ino`): construye el JSON, toma `epoch` por NTP,
   calcula `HMAC_SHA256(HMAC_SECRET, "<ts>.<body>")` con mbedTLS y envía
   `X-Api-Key / X-Timestamp / X-Signature`.
2. **Backend** (`backend/app.py::verify_auth`): rechaza `401` si la API-Key no coincide,
   el timestamp está fuera de ventana o la firma no es idéntica (comparación en tiempo
   constante). Solo el payload verificado llega a `INSERT`.
3. **Escritura dual**: `INSERT INTO readings (...)` (RDS) + `s3_backup()` → objeto
   `s3://<bucket>/raw/<device>/<YYYY-MM-DD>.jsonl` con `ServerSideEncryption=AES256`.
4. **Lectura**: `GET /api/v1/readings` exige API-Key; `/dashboard` es solo lectura y
   marca `EN LÍNEA / CAÍDO` por staleness (`STALE_AFTER_S`, defecto 180 s).

## 3. Gestión de secretos

* **Nunca** en git: `.gitignore` bloquea `*.pem`, `.env`, `*.jsonl`. Verificado con
  `git ls-files | grep -Ei 'pem|/\.env$'` → vacío.
* **Generar**: `python3 -c "import secrets; print(secrets.token_hex(16))"` (API-Key),
  `token_hex(32)` (HMAC).
* **Rotar** (cada 90 días o ante sospecha):
  1. Generar nuevos valores y actualizar el `.env` del EC2 (`/home/ec2-user/iot-poc/deploy/.env`).
  2. `docker-compose up -d --build` (o restart del `api`).
  3. Reflashear la(s) placa(s) con los nuevos valores.
  4. Ventana de corte: los envíos con la clave vieja reciben `401` y se descartan (no se escriben).
* **RDS**: credencial maestra en Secrets Manager (`iot-poc/db`); rotación con
  `aws secretsmanager rotate-secret` cuando se habilite Lambda de rotación.

## 4. Brechas conocidas y plan de endurecimiento (pre-producción)

1. **TLS**: poner Nginx/Caddy con certificado (Let's Encrypt) delante del `:8000`,
   cerrar el `8000/tcp` al mundo y pasar el sketch a `https://` + `WiFiClientSecure` con CA real.
2. **RDS**: `StorageEncrypted=true` (KMS), `DeletionProtection=true`,
   `BackupRetentionPeriod≥7`, `PubliclyAccessible=false` + SG solo desde la EC2.
3. **Claves por dispositivo**: una API-Key/HMAC por `device_id` (tabla o secreto por equipo)
   en vez de una global, para revocar un equipo sin afectar a los demás.
4. **Firma con contador**: agregar `nonce`/monotónico persistido para replay dentro de la ventana de 300 s.
5. **Credenciales AWS en EC2**: migrar de `AWS_*` en `.env` a **Instance Profile (IAM Role)**
   con política mínima (solo `secretsmanager:GetSecretValue` del ARN y `s3:PutObject/GetObject` del bucket).
6. **Trazabilidad**: agregar `id` de correlación placa→RDS→S3 y alertas (p. ej. SNS) cuando el
   dashboard marque `CAÍDO` más de N minutos.

## 5. Respuesta ante incidente

* Clave sospechada → rotar (§3), revisar `readings` por `device_id` en la ventana,
  comparar contra el JSONL de S3 (fuente fría inmutable por día).
* Repo comprometido → el repo no contiene secretos (solo placeholders), basta con rotar
  por precaución y auditar `git log`.
