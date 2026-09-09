# EventBridge: el motor de reglas corre cada minuto (Lambda con el mismo codigo
# de automation/rule_engine.py empaquetado en una zip).
# En dev local lo hace automation/loop.py; en prod evita mantener un contenedor despierto.

resource "aws_lambda_function" "rule_engine" {
  function_name = "${var.project}-rule-engine"
  runtime       = "python3.11"
  handler       = "rule_engine.lambda_handler"
  timeout       = 60
  memory_size   = 256
  role          = aws_iam_role.lambda.arn
  # zip construido por CI: pip install -t . psycopg2-binary && zip rule_engine.zip
  filename         = var.rule_engine_zip
  source_code_hash = filebase64sha256(var.rule_engine_zip)
  environment {
    variables = {
      DB_SECRET_ARN  = aws_secretsmanager_secret.db.arn
      SNS_TOPIC_ARN  = aws_sns_topic.alerts.arn
      STALE_AFTER_S_V2 = "900"
    }
  }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.api.id]
  }
  depends_on = [aws_cloudwatch_log_group.lambda]
}

variable "rule_engine_zip" {
  type        = string
  default     = "../rule_engine.zip"
  description = "Path al zip con rule_engine.py + psycopg2-binary (lo genera el CI)"
}

resource "aws_iam_role" "lambda" {
  name = "${var.project}-lambda-rules"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{ Effect = "Allow", Principal = { Service = "lambda.amazonaws.com" }, Action = "sts:AssumeRole" }]
  })
}
resource "aws_iam_role_policy" "lambda" {
  role = aws_iam_role.lambda.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.db.arn },
      { Effect = "Allow", Action = ["sns:Publish"], Resource = aws_sns_topic.alerts.arn },
      { Effect = "Allow", Action = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface"], Resource = "*" },
      { Effect = "Allow", Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"], Resource = "arn:aws:logs:*:*:*" }
    ]
  })
}

resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.project}-rule-engine"
  retention_in_days = 7
}

resource "aws_cloudwatch_event_rule" "every_minute" {
  name                = "${var.project}-rules-minute"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "rules" {
  rule = aws_cloudwatch_event_rule.every_minute.name
  arn  = aws_lambda_function.rule_engine.arn
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.rule_engine.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.every_minute.arn
}
