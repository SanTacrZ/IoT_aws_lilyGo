# Secrets Manager: creds RDS + claves de la API (rotacion manual 90 dias)
resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.project}/db"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string = jsonencode({
    username = var.db_user
    password = random_password.db.result
    host     = aws_db_instance.main.address
    port     = 5432
    dbname   = var.db_name
  })
}

resource "aws_secretsmanager_secret" "api" {
  name                    = "${var.project}/api-keys"
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "api" {
  secret_id = aws_secretsmanager_secret.api.id
  secret_string = jsonencode({
    DEVICE_API_KEY = "cambiar-al-provisionar-rotar-90d"
    HMAC_SECRET    = "cambiar-al-provisionar-rotar-90d"
    ADMIN_KEY      = "cambiar-al-provisionar-rotar-90d"
  })
}

# S3 backup frio (raw JSONL diario) con lifecycle a Glacier
resource "aws_s3_bucket" "raw" {
  bucket        = "${var.project}-${data.aws_caller_identity.current.account_id}-raw"
  force_destroy = false
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  bucket = aws_s3_bucket.raw.id
  rule {
    id     = "to-glacier"
    status = "Enabled"
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }
}
