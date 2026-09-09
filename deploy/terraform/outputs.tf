# Salidas para conectar el resto del sistema (sin secretos!)
output "rds_endpoint" { value = aws_db_instance.main.address }
output "secret_db_arn" { value = aws_secretsmanager_secret.db.arn }
output "secret_api_arn" { value = aws_secretsmanager_secret.api.arn }
output "alb_dns" { value = aws_lb.api.dns_name }
output "ecr_repo" { value = aws_ecr_repository.api.repository_url }

# ---- Post-instalacion (manual, una vez) ----
# 1. Rotar las 3 claves del secreto agrosense-lab/api-keys
# 2. aws ecr login + build/push de la imagen
# 3. Migraciones via ECS RunTask: python -u migrate_prod.py
# 4. Datos demo (opcional): python -u burn_prod.py
# 5. Prod real: HTTPS con ACM + NAT gateway + roles dedicados
