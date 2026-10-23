# Private IP of the server:
output "private_ip" {
  value = aws_instance.this.private_ip
}

# ID of the data volume (Ansible finds the disk by it):
output "data_volume_id" {
  value = aws_ebs_volume.data.id
}

# ID of the backup volume, or null when the server has none:
output "backup_volume_id" {
  value = one(aws_ebs_volume.backup[*].id)
}

# Security group of the server (e.g. to open a port to another server later):
output "security_group_id" {
  value = aws_security_group.this.id
}
