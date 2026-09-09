# RDS PostgreSQL 16 con TimescaleDB (shared_preload_libraries)
resource "aws_db_parameter_group" "timescale" {
  name   = "${var.project}-timescale-pg16"
  family = "postgres16"
  parameter {
    name  = "shared_preload_libraries"
    value = "timescaledb"
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
  identifier             = "${var.project}-db"
  engine                 = "postgres"
  engine_version         = "16.4"
  instance_class         = "db.t4g.micro" # subir a medium al crecer
  allocated_storage      = 20
  storage_type           = "gp3"
  db_name                = var.db_name
  username               = var.db_user
  password               = random_password.db.result
  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.timescale.name
  publicly_accessible    = false            # NUNCA publico
  multi_az               = false            # true en produccion seria
  backup_retention_period = 7
  copy_tags_to_snapshot   = true
  deletion_protection     = true
  skip_final_snapshot     = false
  final_snapshot_identifier = "${var.project}-final"
  tags = { Name = "${var.project}-rds" }
}
