# Written by terraform apply (lab: mysql-single). Rewritten on every apply, do not edit.
# Host names are the aliases from ~/.ssh/aws-mysql-single.conf (IPs, user, key, jump via mysql-pmm).

[pmm_server]
mysql-pmm private_ip=${pmm_server_private_ip}

[mysql]
mysql-source private_ip=${mysql_internal_ip} data_volume_id=${mysql_data_volume_id} backup_volume_id=${mysql_backup_volume_id}

# Empty unless terraform apply -var mysql_replica=true:
[mysql_replica]
%{ if mysql_replica_ip != null ~}
mysql-replica private_ip=${mysql_replica_ip} data_volume_id=${mysql_replica_data_volume_id} backup_volume_id=${mysql_replica_backup_volume_id}
%{ endif ~}

[all:vars]
ansible_user=${ssh_user}
vpc_cidr_block=${vpc_cidr_block}
backup_bucket=${backup_bucket}
backup_bucket_region=${backup_bucket_region}
