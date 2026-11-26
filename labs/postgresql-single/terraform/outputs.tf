
# Useful values after terraform apply (also written to the SSH config and the Ansible inventory):
########################################################################################################

output "postgresql_ec2_instance_internal_ip" {
  value = module.postgresql_ec2_instance.private_ip
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

# Grafana (next to PMM): log in as admin with the password from ../ansible/credentials/grafana_admin_password
output "grafana_url" {
  value = "http://${module.pmm_server.pmm_server_public_ip}:3000"
}

output "prometheus_url" {
  value = "http://${module.pmm_server.pmm_server_public_ip}:9090"
}

# Backups: what pgBackRest needs to reach its repository (repo1-s3-bucket, repo1-s3-region; the server's role
# gives the credentials: repo1-s3-key-type=auto)
output "backup_bucket" {
  value = module.backup_bucket.bucket_name
}

output "backup_bucket_region" {
  value = module.backup_bucket.bucket_region
}

output "backup_folder" {
  description = "pgBackRest's folder; one repository per cluster below it (repo1-path). The pg_dump archives are in pg_dump/."
  value       = "/pgbackrest"
}

# Secrets Manager: the lab's secrets, readable by the database server (the values are not in the state)
output "secrets" {
  value = module.lab_secrets.names
}

output "postgresql_iam_role" {
  value = module.postgresql_role.role_name
}
