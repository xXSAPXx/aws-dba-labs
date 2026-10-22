
####################################################################################################
# Files written on your machine on every apply (and removed on destroy):
#   ~/.ssh/aws-mysql-single.conf     SSH config: ssh mysql-pmm / ssh mysql-source (via the PMM server)
#   ../ansible/inventory.ini         Ansible inventory: the same host names + private IPs + VPC CIDR
# The host names differ from postgresql-single's, so both labs can run at the same time.
####################################################################################################

resource "local_file" "ssh_config" {
  filename             = pathexpand("~/.ssh/aws-mysql-single.conf")
  file_permission      = "0600"
  directory_permission = "0700"
  content = templatefile("${path.module}/ssh_config.tpl", {
    pmm_server_public_ip = module.pmm_server.pmm_server_public_ip
    mysql_internal_ip    = module.mysql_ec2_instance.private_ip
    ssh_user             = local.ssh_user
    ssh_private_key_path = var.ssh_private_key_path
  })
}

resource "local_file" "ansible_inventory" {
  filename             = "${path.module}/../ansible/inventory.ini"
  file_permission      = "0644"
  directory_permission = "0755"
  content = templatefile("${path.module}/ansible_inventory.tpl", {
    pmm_server_private_ip  = module.pmm_server.pmm_server_private_ip
    mysql_internal_ip      = module.mysql_ec2_instance.private_ip
    mysql_data_volume_id   = module.mysql_ec2_instance.data_volume_id
    mysql_backup_volume_id = module.mysql_ec2_instance.backup_volume_id
    vpc_cidr_block         = module.vpc.vpc_cidr_block
    backup_bucket          = module.backup_bucket.bucket_name
    backup_bucket_region   = module.backup_bucket.bucket_region
    ssh_user               = local.ssh_user
  })
}

# New servers come with new SSH host keys: forget the old ones whenever the servers change.
resource "terraform_data" "reset_known_hosts" {
  triggers_replace = [
    module.pmm_server.pmm_server_public_ip,
    module.mysql_ec2_instance.private_ip,
  ]

  provisioner "local-exec" {
    command = "rm -f ~/.ssh/aws-mysql-single_known_hosts"
  }
}
