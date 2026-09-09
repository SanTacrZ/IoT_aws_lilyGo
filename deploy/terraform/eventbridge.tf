# EventBridge: el motor de reglas corre cada minuto (Lambda con el mismo codigo
# de automation/rule_engine.py empaquetado en zip). OFF por defecto en el lab:
# requiere build del zip (var.rule_engine_zip). En dev local lo hace automation/loop.py.

variable "deploy_rule_engine" {
  type        = bool
  default     = false
  description = "Activar lambda+schedule (requiere zip del rule engine)"
}
variable "rule_engine_zip" {
  type        = string
  default     = ""
  description = "Path al zip con rule_engine.py + psycopg2 (lo genera el CI)"
}

resource "aws_lambda_function" "rule_engine" {
  count            = var.deploy_rule_engine ? 1 : 0
  function_name    = "${var.project}-rule-engine"
  runtime          = "python3.11"
  handler          = "rule_engine.lambda_handler"
  timeout          = 60
  memory_size      = 256
  role             = data.aws_iam_role.lab.arn
  filename         = var.rule_engine_zip != "" ? var.rule_engine_zip : null
  source_code_hash = var.rule_engine_zip != "" ? filebase64sha256(var.rule_engine_zip) : null
  environment {
    variables = {
      DB_SECRET_ARN    = aws_secretsmanager_secret.db.arn
      SNS_TOPIC_ARN    = aws_sns_topic.alerts.arn
      STALE_AFTER_S_V2 = "900"
    }
  }
  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.api.id]
  }
}

resource "aws_cloudwatch_log_group" "lambda" {
  count             = var.deploy_rule_engine ? 1 : 0
  name              = "/aws/lambda/${var.project}-rule-engine"
  retention_in_days = 7
}

resource "aws_cloudwatch_event_rule" "every_minute" {
  count               = var.deploy_rule_engine ? 1 : 0
  name                = "${var.project}-rules-minute"
  schedule_expression = "rate(1 minute)"
}

resource "aws_cloudwatch_event_target" "rules" {
  count = var.deploy_rule_engine ? 1 : 0
  rule  = aws_cloudwatch_event_rule.every_minute[0].name
  arn   = aws_lambda_function.rule_engine[0].arn
}

resource "aws_lambda_permission" "eventbridge" {
  count         = var.deploy_rule_engine ? 1 : 0
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.rule_engine[0].function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.every_minute[0].arn
}
