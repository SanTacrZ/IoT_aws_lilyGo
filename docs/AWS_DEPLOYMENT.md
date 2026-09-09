# Despliegue en AWS — Fase 4 (producción comunitaria)

## Objetivo
Migrar el POC (docker-compose local) a AWS sin reescribir la lógica: el mismo `app_v2.py`
corre en ECS Fargate contra RDS PostgreSQL.

## Topología objetivo

```
Route53 ──> ALB (HTTPS, ACM cert)
              │
              ├── target: ECS Fargate (contenedor Flask: app.py + app_v2.py)
              │        └── VPC privada ──> RDS PostgreSQL 16 (subnet privado, no público)
              │                                   └── TimescaleDB extension
              ├── CloudWatch Logs/Alarms
              ├── Secrets Manager (DB creds, HMAC_SECRET, DEVICE_API_KEY)
              └── S3 (backup raw JSONL diario, lifecycle → Glacier 90d)
```

## Pasos (orden recomendado)

1. **Red:** 1 VPC, 2 AZ, subnets públicas (ALB) + privadas (RDS/Fargate).
2. **RDS:** PostgreSQL 16, db.t4g.micro (empezar Single-AZ), storage gp3, automated backups 7d,
   encryption KMS. Parameter group: `shared_preload_libraries = timescaledb`.
   `CREATE EXTENSION timescaledb;` y correr bloque Timescale de `002_precision.sql`.
3. **Secrets:** guardar `{username,password,host,port,dbname}` en Secrets Manager (el backend
   ya lo lee con `DB_SECRET_ARN`) y rotar `HMAC_SECRET`/`DEVICE_API_KEY` cada 90 días.
4. **ECR + imagen:** Dockerfile existente en `iot-poc/backend/`. CI: GitHub Actions → build →
   push ECR → `aws ecs update-service --force-new-deployment`.
5. **ECS Fargate:** task 0.25 vCPU / 512 MB, 2 tareas mínimas, health check `/health`.
6. **Scheduler:** EventBridge rule `rate(1 minute)` → Lambda `rule_engine` (el mismo motor de
   reglas aislado en `automation/rule_engine.py`, reutilizable).
7. **SNS topic** `agrosense-alerts` con suscripciones SMS/email de los comuneros.
8. **Dominio + TLS:** Route53 + ACM (gratis) en el ALB. Nunca exponer RDS.

## IaC (esqueleto Terraform) — `deploy/terraform/`
```
main.tf       provider aws, vpc module
rds.tf        db instance + parameter group + secret
ecs.tf        cluster, task def, service, ALB
sns.tf        topic + subscriptions
eventbridge.tf  rule + lambda rule_engine
```
(Ejecutar por fases; primero rds.tf + ecs.tf.)

## Migración de datos del POC
1. `pg_dump` local → restore en RDS.
2. Correr `002_precision.sql` (idempotente).
3. Backfill: `INSERT INTO farms/zones` desde datos conocidos; asignar `devices_v2.zone_id`.

## Seguridad checklist (complementa docs/SECURITY.md)
- [ ] RDS en subnets privadas, security group solo desde SG de Fargate
- [ ] IAM task role mínimo (secretsmanager:GetSecretValue, s3:PutObject al bucket raw)
- [ ] ALB: HTTPS only, TLS1.2+, security headers (ya en el backend)
- [ ] WAF opcional (rate limit por IP en /api/*)
- [ ] Backups: RDS automated + snapshot antes de cada migración
- [ ] CloudWatch alarm: 5xx del ALB, CPU RDS >80%, storage <10% ( Growth alarm)

## Costos de arranque (aprox, us-west-2)
| Recurso | Mes |
|---|---|
| RDS db.t4g.micro + 20GB | ~$15 |
| Fargate 0.25vCPU×2 tareas | ~$18 |
| ALB | ~$17 |
| S3+Secrets+SNS (poco uso) | ~$3 |
| **Total** | **~$53/mes** (bajar a ~$35 con 1 tarea y ALB compartido, o ~$5 con Lambda+API GW en ingest baja) |
