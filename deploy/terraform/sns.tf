# SNS: alertas por SMS/email a los comuneros
resource "aws_sns_topic" "alerts" {
  name              = "${var.project}-alerts"
  display_name      = "AgroSense Alertas"
  kms_master_key_id = "alias/aws/sns"
}

variable "alert_phone_numbers" {
  type        = list(string)
  default     = []      # ["+52XXXXXXXXXX", ...] numeros de los comuneros
  description = "Suscripciones SMS al topic de alertas"
}

resource "aws_sns_topic_subscription" "sms" {
  count     = length(var.alert_phone_numbers)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "sms"
  endpoint  = var.alert_phone_numbers[count.index]
}

# Email (opcional, requiere confirmar en el correo)
variable "alert_emails" {
  type    = list(string)
  default = []
}
resource "aws_sns_topic_subscription" "email" {
  count     = length(var.alert_emails)
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_emails[count.index]
}

output "sns_topic_arn" { value = aws_sns_topic.alerts.arn }
