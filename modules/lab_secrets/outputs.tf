output "names" {
  description = "The full name of every secret, by its key in var.secrets."
  value       = { for key, secret in aws_secretsmanager_secret.this : key => secret.name }
}
