# ECS Fargate: el mismo contenedor del dev (Dockerfile.dev) corriendo app_v2
# Imagen: ECR repo + CI (GitHub Actions) hace build/push; el service apunta al latest.

resource "aws_ecr_repository" "api" {
  name         = "${var.project}-api"
  force_delete = false
  image_tag_mutability = "IMMUTABLE"
}

resource "aws_iam_role" "exec" {
  name = "${var.project}-ecs-exec"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy_attachment" "exec" {
  role       = aws_iam_role.exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# Task role: leer secretos y escribir al bucket raw
resource "aws_iam_role" "task" {
  name = "${var.project}-ecs-task"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "task" {
  role = aws_iam_role.task.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"],
        Resource = [aws_secretsmanager_secret.db.arn, aws_secretsmanager_secret.api.arn] },
      { Effect = "Allow", Action = ["s3:PutObject"], Resource = "${aws_s3_bucket.raw.arn}/raw/*" },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn }
    ]
  })
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${var.project}-api"
  retention_in_days = 14
}

resource "aws_ecs_cluster" "main" { name = "${var.project}-cluster" }

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.project}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256" # 2 tareas x 0.25 vCPU ~ el sizing del benchmark local
  memory                   = "512"
  execution_role_arn       = aws_iam_role.exec.arn
  task_role_arn            = aws_iam_role.task.arn
  container_definitions = jsonencode([{
    name      = "api"
    image     = "${aws_ecr_repository.api.repository_url}:latest"
    essential = true
    command   = ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--threads", "4", "app_v2:app"]
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
      options = { "awslogs-group" = aws_cloudwatch_log_group.api.name,
                  "awslogs-region" = var.region, "awslogs-stream-prefix" = "api" }
    }
    healthCheck = {
      command  = ["CMD-SHELL", "python -c 'import urllib.request; urllib.request.urlopen(\"http://localhost:8000/health\", timeout=4)'"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 20
    }
  }])
}

resource "aws_lb" "api" {
  name               = "${var.project}-api"
  internal           = false
  security_groups    = [aws_security_group.alb.id]
  subnets            = aws_subnet.public[*].id
  enable_http2       = true
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

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.api.arn
  port              = 443
  protocol          = "HTTPS"
  # ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06" (requiere cert ACM: domain_name var)
  certificate_arn = var.acm_cert_arn
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

variable "acm_cert_arn" {
  type        = string
  description = "ARN del certificado ACM de tu dominio (crear en la consola y validar DNS)"
}

resource "aws_ecs_service" "api" {
  name            = "${var.project}-api"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = 2
  launch_type     = "FARGATE"
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.api.id]
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = var.api_port
  }
  depends_on = [aws_lb_listener.https]
}
