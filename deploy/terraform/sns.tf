# SNS: alertas por SMS/email a los comuneros
resource "aws_sns_topic" "alerts" {
  name              = "${var.project}-alerts"
  display_name      = "AgroSense Alertas"
  kms_master_key_id = "alias/aws/sns"
}

variable "alert_phone_numbers" {
  type        = list(string)
  default     = []
  description = "Suscripciones SMS al topic de alertas"
}

resource "aws_sns_topic_subscription" "sms" {
  count     = length(var.alert_phone_numbers)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "sms"
  endpoint  = var.alert_phone_numbers[count.index]
}

output "sns_topic_arn" {
  value = aws_sns_topic.alerts.arn
}
