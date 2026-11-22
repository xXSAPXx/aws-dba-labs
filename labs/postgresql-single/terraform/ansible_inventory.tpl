# Written by terraform apply (lab: postgresql-single). Rewritten on every apply, do not edit.
# Host names are the aliases from ~/.ssh/aws-postgresql-lab.conf (IPs, user, key, jump via pmm-server).

[pmm_server]
pmm-server private_ip=${pmm_server_private_ip}

[postgresql]
postgresql-source private_ip=${postgresql_internal_ip} data_volume_id=${postgresql_data_volume_id}

# pgBackRest's repository (role pgbackrest)
[postgresql:vars]
backup_bucket=${backup_bucket}
backup_bucket_region=${backup_bucket_region}

[all:vars]
ansible_user=${ssh_user}
vpc_cidr_block=${vpc_cidr_block}
