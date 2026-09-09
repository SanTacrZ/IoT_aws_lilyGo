# Secrets Manager: creds RDS + claves de la API (aleatorias, rotar 90 dias)
resource "aws_secretsmanager_secret" "db" {
  name                    = "${var.project}-lab/db"
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
  name                    = "${var.project}-lab/api-keys"
  recovery_window_in_days = 7
}

resource "random_password" "apikey" {
  length  = 40
  special = false
}
resource "random_password" "hmac" {
  length  = 64
  special = false
}
resource "random_password" "admin" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret_version" "api" {
  secret_id = aws_secretsmanager_secret.api.id
  secret_string = jsonencode({
    DEVICE_API_KEY = random_password.apikey.result
    HMAC_SECRET    = random_password.hmac.result
    ADMIN_KEY      = random_password.admin.result
  })
}

# S3 backup frio (raw JSONL) con lifecycle a Glacier — opcional:
# el lab deniega S3 via SCP (deploy_s3=false); en cuenta propia: true
resource "aws_s3_bucket" "raw" {
  count         = var.deploy_s3 ? 1 : 0
  bucket        = "${var.project}-${data.aws_caller_identity.current.account_id}-raw"
  force_destroy = false
}

resource "aws_s3_bucket_lifecycle_configuration" "raw" {
  count  = var.deploy_s3 ? 1 : 0
  bucket = try(aws_s3_bucket.raw[0].id, "")
  rule {
    id     = "to-glacier"
    status = "Enabled"
    filter {}
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }
}
