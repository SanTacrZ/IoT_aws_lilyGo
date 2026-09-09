# Salidas para conectar el resto del sistema
output "rds_endpoint"   { value = aws_db_instance.main.address }
output "secret_db_arn"  { value = aws_secretsmanager_secret.db.arn }
output "secret_api_arn" { value = aws_secretsmanager_secret.api.arn }
output "alb_dns"        { value = aws_lb.api.dns_name }
output "ecr_repo"       { value = aws_ecr_repository.api.repository_url }
output "sns_topic_arn"  { value = aws_sns_topic.alerts.arn }

# ---- Recordatorio de post-instalacion (manual, una vez) ----
# 1. psql -h <rds_endpoint> -U iot -d iot -f db/migrations/002_precision.sql
# 2. CREATE EXTENSION timescaledb; + bloque timescale del seed
# 3. Rotar las 3 claves del secreto ${var.project}/api-keys
# 4. Configurar dominio Route53 -> ALB
# 5. aws ecr login + build/push de la imagen (CI)
