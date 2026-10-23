# Written by terraform apply (lab: mysql-single). Rewritten on every apply, do not edit.
# The PMM server is the jump host (ProxyJump): it only relays the connection, your key stays on your machine.
# Keepalives: idle connections (e.g. during a long package install) aren't dropped by NAT, and a dead
# connection is detected within a minute instead of hanging forever.

Host mysql-pmm
    HostName ${pmm_server_public_ip}
    User ${ssh_user}
%{ if ssh_private_key_path != null ~}
    IdentityFile "${ssh_private_key_path}"
%{ endif ~}
    UserKnownHostsFile ~/.ssh/aws-mysql-single_known_hosts
    StrictHostKeyChecking accept-new
    ServerAliveInterval 15
    ServerAliveCountMax 4

Host mysql-source
    HostName ${mysql_internal_ip}
    User ${ssh_user}
%{ if ssh_private_key_path != null ~}
    IdentityFile "${ssh_private_key_path}"
%{ endif ~}
    UserKnownHostsFile ~/.ssh/aws-mysql-single_known_hosts
    StrictHostKeyChecking accept-new
    ServerAliveInterval 15
    ServerAliveCountMax 4
    ProxyJump mysql-pmm
