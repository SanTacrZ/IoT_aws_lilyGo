# ECS Fargate: el mismo contenedor del dev (Dockerfile.dev) corriendo app_v2.
# Lab: usa LabRole pre-existente (no se pueden crear roles).
# ECS Exec habilitado = consola administrada sin SSH.

resource "aws_ecr_repository" "api" {
  name         = "${var.project}-api"
  force_delete = false
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.project}-api"
  retention_in_days = 14
}

resource "aws_ecs_cluster" "main" {
  name = "${var.project}-cluster"
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = data.aws_iam_role.lab.arn
  task_role_arn            = data.aws_iam_role.lab.arn
  container_definitions = jsonencode([{
    name         = "api"
    image        = "${aws_ecr_repository.api.repository_url}:latest"
    essential    = true
    command      = ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--threads", "4", "app_v2:app"]
    portMappings = [{ containerPort = var.api_port }]
    environment = [
      { name = "DB_SECRET_ARN", value = aws_secretsmanager_secret.db.arn },
      { name = "AWS_REGION", value = var.region }
    ]
    secrets = [
      { name = "DEVICE_API_KEY", valueFrom = "${aws_secretsmanager_secret.api.arn}:DEVICE_API_KEY::" },
      { name = "HMAC_SECRET", valueFrom = "${aws_secretsmanager_secret.api.arn}:HMAC_SECRET::" },
      { name = "ADMIN_KEY", valueFrom = "${aws_secretsmanager_secret.api.arn}:ADMIN_KEY::" }
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.region
        "awslogs-stream-prefix" = "api"
      }
    }
    healthCheck = {
      command     = ["CMD-SHELL", "python -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8000/health\", timeout=4)'"]
      interval    = 30
      timeout     = 5
      retries     = 3
      startPeriod = 20
    }
  }])
}

resource "aws_lb" "api" {
  name            = "${var.project}-api"
  internal        = false
  security_groups = [aws_security_group.alb.id]
  subnets         = aws_subnet.public[*].id
  enable_http2    = true
}

resource "aws_lb_target_group" "api" {
  name        = "${var.project}-api"
  port        = var.api_port
  protocol    = "HTTP"
  vpc_id      = aws_vpc.main.id
  target_type = "ip"
  health_check {
    path    = "/health"
    matcher = "200"
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.api.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_ecs_service" "api" {
  name            = "${var.project}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 1
  # ECS Exec: "SSH equivalente" para entrar a los contenedores Fargate
  enable_execute_command = true
  launch_type            = "FARGATE"
  network_configuration {
    subnets          = aws_subnet.public[*].id
    security_groups  = [aws_security_group.api.id]
    assign_public_ip = true
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_port
  }
  depends_on = [aws_lb_listener.http]
}
