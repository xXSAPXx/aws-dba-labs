
# Useful values after terraform apply (also written to the SSH config and the Ansible inventory):
########################################################################################################

output "mysql_ec2_instance_internal_ip" {
  value = module.mysql_ec2_instance.private_ip
}

output "pmm_server_public_ip" {
  value = module.pmm_server.pmm_server_public_ip
}

output "pmm_server_private_ip" {
  value = module.pmm_server.pmm_server_private_ip
}

# PMM UI: log in as admin with the password from ../ansible/credentials/pmm_admin_password
output "pmm_url" {
  value = "https://${module.pmm_server.pmm_server_public_ip}"
}

# Grafana: log in as admin with the password from ../ansible/credentials/grafana_admin_password
output "grafana_url" {
  value = "http://${module.pmm_server.pmm_server_public_ip}:3000"
}

# Prometheus: no login, reachable from your IP only (admin_cidr)
output "prometheus_url" {
  value = "http://${module.pmm_server.pmm_server_public_ip}:9090"
}

# Backups: where the server may write in S3 (its IAM role gives the credentials, no keys on the server)
output "backup_bucket" {
  value = module.backup_bucket.bucket_name
}

output "backup_bucket_region" {
  value = module.backup_bucket.bucket_region
}

output "backup_s3_prefix" {
  description = "The folder the database server may use, e.g. s3://<bucket>/mysql/full/..."
  value       = "s3://${module.backup_bucket.bucket_name}/mysql/"
}

output "mysql_iam_role" {
  value = module.mysql_role.role_name
}
