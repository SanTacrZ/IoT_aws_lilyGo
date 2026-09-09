# Reporte de Despliegue en AWS — AgroSense (clase)

> Fecha: 2026-09-09 · Cuenta lab: 265096288210 (AWS Academy, región us-east-1) · rama `ejercicio-3`

## Resultado

El sistema quedó **desplegado y verificado de punta a punta en AWS** y luego destruido
para no consumir créditos del lab. Redespliegue completo: **~10 minutos**.

### URL en vivo durante la demo
```
http://agrosense-api-718820605.us-east-1.elb.amazonaws.com/dashboard-v2
```

### Verificación e2e (evidencia)
| Prueba | Resultado |
|---|---|
| `/health` | `{"api":"v2","db":"up","status":"ok"}` |
| Ingesta firmada HMAC (3 sensores) | `{"status":"stored","count":3}` |
| Registro autónomo en `/api/v2/state` | `lilygo-aws-01 online` |
| Migraciones vía ECS RunTask | tablas + MV horaria + pg_cron ✅ |

### Infraestructura creada (Terraform, 27 recursos)
```
Route:  ALB (http:80) → TargetGroup(:8000) health /health
Computo: ECS cluster agrosense-cluster + Fargate 1×0.25vCPU/512MB (imagen desde ECR, ECS Exec ON)
Datos:   RDS PostgreSQL 16.15 db.t4g.micro (subnets privadas, SG solo desde Fargate)
Secrets: agrosense-lab/db (creds RDS) · agrosense-lab/api-keys (3 claves aleatorias)
Alertas: SNS topic agrosense-alerts (suscripciones SMS/email opcionales)
Red:     VPC /16, 2 public (ALB+Fargate con IP pública) + 2 private (RDS)
```

## Costos medidos (lab)

| Recurso | $/hora | $/día |
|---|---|---|
| RDS db.t4g.micro | ~0.025 | ~0.60 |
| ALB + LCU base | ~0.025 | ~0.60 |
| Fargate 0.25vCPU×1 | ~0.010 | ~0.25 |
| **Total demo** | **~0.06** | **~1.45** |

Recomendación aplicada: `desired_count=1`, sin NAT (ahorra ~$40/mes), S3/Lambda desactivados
por SCP. **Destroy al terminar la demo** — los créditos del lab se conservan.

## Limitaciones del lab encontradas y cómo se resolvieron

| Problema | Solución aplicada |
|---|---|
| SCP deniega `s3:GetBucketObjectLockConfiguration` | S3 backup opcional (`deploy_s3=false`) |
| IAM no permite crear roles | data-source `LabRole` pre-existente |
| RDS no soporta TimescaleDB (lista cerrada de extensiones) | PostgreSQL vanilla + **MV horaria + pg_cron** |
| Versión de motor 16.4 no disponible en el lab | 16.15 (`describe-db-engine-versions`) |
| Parámetro estático rechaza apply inmediato | `apply_method = "pending-reboot"` |
| Secretos en ventana de borrado de 7 días tras destroy | nombres versionados (`agrosense-lab/*`) |
| Secretos vs 12-factor | `app_v2.py` lee `DB_SECRET_ARN` (boto3) con cache en memoria |
| Lección de proceso | nunca lanzar 2 `terraform apply` en paralelo (se corrió `destroy` completo y apply limpio) |

## Runbook de redespliegue (próxima clase, ~10 min)

```bash
# 1) Credenciales frescas del Learner Lab (AWS Details → Copy AWS CLI credentials)
export AWS_ACCESS_KEY_ID=... AWS_SECRET_ACCESS_KEY=... AWS_SESSION_TOKEN=... AWS_DEFAULT_REGION=us-east-1

# 2) Infraestructura (~8 min: RDS es lo lento)
cd iot-poc/deploy/terraform && terraform apply -auto-approve
#    → outputs: alb_dns, ecr_repo, secret_db_arn, secret_api_arn

# 3) Imagen
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account>.dkr.ecr.us-east-1.amazonaws.com
docker build -f iot-poc/backend/Dockerfile.dev -t agrosense-api:prod .
docker tag agrosense-api:prod <ecr_repo>:latest && docker push <ecr_repo>:latest

# 4) Migraciones (tarea one-shot en la VPC)
SUBS=$(aws ec2 describe-subnets --filters Name=tag:Name,Values=agrosense-public-* --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
SG=$(aws ec2 describe-security-groups --filters Name=tag:Name,Values=agrosense-api --query 'SecurityGroups[0].GroupId' --output text)
aws ecs run-task --cluster agrosense-cluster --launch-type FARGATE --task-definition agrosense-api \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides '{"containerOverrides":[{"name":"api","command":["python","-u","migrate_prod.py"]}]}'

# 5) Datos demo (opcional, misma mecánica con "python -u burn_prod.py")

# 6) Claves: aws secretsmanager get-secret-value --secret-id agrosense-lab/api-keys
# 7) Probar: curl http://<alb_dns>/health y el dashboard con la DEVICE_API_KEY

# ---- Al terminar la clase (AHORRA CRÉDITOS) ----
terraform destroy -auto-approve   # ~12 min (RDS es lento)
```

## Firmware apuntando a la nube (cuando haya hardware en campo)

En el sketch elegido, 4 líneas:
```cpp
const char* API_URL = "http://<alb_dns>/api/v2/readings";
const char* API_KEY = "<DEVICE_API_KEY del secreto>";
const char* HMAC_SECRET = "<HMAC_SECRET del secreto>";
// (prod real: https + WiFiClientSecure)
```

## Pendientes para la próxima clase
1. Redespliegue por runbook + `burn_prod.py` + simulador contra ALB (dashboard vivo)
2. SNS con un número real para SMS de alertas
3. Fase 6: ETc (OpenWeather + FAO-56) para riego por evapotranspiración
4. Certificado ACM + dominio para HTTPS (el ALB actual es HTTP de laboratorio)
