# AWS Variables:
variable "aws_key_pair" {
  type        = string
  sensitive   = true
  description = "SSH KeyPair for the EC2 instances"
}

variable "admin_cidr" {
  type        = string
  description = "Your public IP in CIDR form (e.g. 203.0.113.10/32). Only this range can reach SSH and the PMM UI."

  validation {
    condition     = can(cidrhost(var.admin_cidr, 0))
    error_message = "admin_cidr must be a valid CIDR block, e.g. 203.0.113.10/32."
  }
}

# Instance types: Rocky Linux 10 needs x86-64-v3 CPUs, so use a current (Nitro) type such as t3 or m7i, not t2.
# t3 is burstable (see cpu_credits below). For heavy load tests use a non-burstable type, e.g. m7i.large (2 vCPU, 8 GB).
variable "mysql_instance_type" {
  type        = string
  default     = "t3.small"
  description = "EC2 instance type of the MySQL server"

  validation {
    condition     = !startswith(var.mysql_instance_type, "t2.")
    error_message = "Rocky Linux 10 needs a current instance type (t3, m7i, ...), not t2."
  }
}

variable "pmm_instance_type" {
  type        = string
  default     = "t3.medium"
  description = "EC2 instance type of the PMM server, which is also the load generator (PMM alone needs at least 2 GB RAM)"

  validation {
    condition     = !startswith(var.pmm_instance_type, "t2.")
    error_message = "Rocky Linux 10 needs a current instance type (t3, m7i, ...), not t2."
  }
}

variable "pmm_volume_size" {
  type        = number
  default     = 20
  description = "GB of the PMM server's root disk: the PMM image (3.6 GB) and PMM's data"
}

# A t3 may use 20% of each vCPU continuously and more in bursts, paid for with CPU credits. It starts with none.
#   unlimited: never throttled. CPU above the 20% is billed at $0.05 per vCPU-hour: about a cent per hour
#              with the default workload, at most $0.08 per hour for a server that runs flat out.
#   standard:  fixed price. A server without credits is throttled to the 20%, which shows up in PMM as a slow
#              database.
variable "cpu_credits" {
  type        = string
  default     = "unlimited"
  description = "CPU credit mode of the burstable (t3) servers: unlimited or standard"

  validation {
    condition     = contains(["unlimited", "standard"], var.cpu_credits)
    error_message = "cpu_credits must be \"unlimited\" or \"standard\"."
  }
}

variable "mysql_data_volume_size" {
  type        = number
  default     = 20
  description = "Size in GB of the MySQL data volume (gp3, mounted at /var/lib/mysql: data and binlogs). Can be grown in place later."
}

variable "mysql_backup_volume_size" {
  type        = number
  default     = 20
  description = "Size in GB of the backup volume (gp3, mounted at /backup): XtraBackup and MySQL Shell backups before they go to S3."
}

variable "ssh_private_key_path" {
  type        = string
  default     = null
  description = "Path to the key pair's private key, written into ~/.ssh/aws-mysql-single.conf. Leave unset to use ssh-agent / your default keys."
}
