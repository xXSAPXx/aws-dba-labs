###################################################################################
# Secrets Manager secrets of a lab, e.g. the backup encryption key: random values made by AWS, readable by the
# lab's servers through their IAM roles. Read one (on a server, or with your admin credentials):
#   aws secretsmanager get-secret-value --secret-id <name> --query SecretString --output text
#
# The values never enter Terraform's plan or state: they are write-only (secret_string_wo, Terraform 1.11+).
# Deleted at once with the lab, without the usual 7-30 day recovery window, so the next deploy can create the
# same names again. Never do that in production: a deleted backup key makes every backup made with it unreadable.
###################################################################################

resource "aws_secretsmanager_secret" "this" {
  for_each = var.secrets

  name                    = "${var.name_prefix}/${each.key}"
  description             = each.value.description
  recovery_window_in_days = 0
}

# A new random value on every plan, but only sent to AWS when secret_string_wo_version changes.
ephemeral "aws_secretsmanager_random_password" "this" {
  for_each = var.secrets

  password_length     = each.value.length
  exclude_punctuation = true # letters and digits only: safe in files, shell variables and command options
}

resource "aws_secretsmanager_secret_version" "this" {
  for_each = var.secrets

  secret_id        = aws_secretsmanager_secret.this[each.key].id
  secret_string_wo = ephemeral.aws_secretsmanager_random_password.this[each.key].random_password
  # Raise it to store a new random value (a rotation; what the old value encrypted needs the old value).
  secret_string_wo_version = 1
}

# The lab's servers may read these secrets and nothing else in Secrets Manager.
data "aws_iam_policy_document" "read" {
  statement {
    sid       = "ReadTheLabSecrets"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = [for secret in aws_secretsmanager_secret.this : secret.arn]
  }
}

resource "aws_iam_role_policy" "read" {
  for_each = toset(var.reader_role_names)

  name   = "lab-secrets"
  role   = each.value
  policy = data.aws_iam_policy_document.read.json
}
