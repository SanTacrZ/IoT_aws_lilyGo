# RDS PostgreSQL 16 vanilla (RDS gestionado NO soporta TimescaleDB).
# Estrategia prod: MV horaria + refresh automatico con pg_cron
# (ver iot-poc/backend/migrate_prod.py). TimescaleDB se queda para dev (Docker).
resource "aws_db_parameter_group" "pg16" {
  name   = "${var.project}-pg16"
  family = "postgres16"
  parameter {
    name         = "shared_preload_libraries"
    value        = "pg_stat_statements,pg_cron"
    apply_method = "pending-reboot"
  }
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-dbsubnets"
  subnet_ids = aws_subnet.private[*].id
}

resource "random_password" "db" {
  length  = 24
  special = false
}

resource "aws_db_instance" "main" {
  identifier              = "${var.project}-db"
  engine                  = "postgres"
  engine_version          = "16.15"
  instance_class          = "db.t4g.micro"
  allocated_storage       = 20
  storage_type            = "gp3"
  db_name                 = var.db_name
  username                = var.db_user
  password                = random_password.db.result
  db_subnet_group_name    = aws_db_subnet_group.main.name
  vpc_security_group_ids  = [aws_security_group.rds.id]
  parameter_group_name    = aws_db_parameter_group.pg16.name
  publicly_accessible     = false
  multi_az                = false
  backup_retention_period = 7
  copy_tags_to_snapshot   = true
  deletion_protection     = false
  skip_final_snapshot     = true
  tags                    = { Name = "${var.project}-rds" }
}
