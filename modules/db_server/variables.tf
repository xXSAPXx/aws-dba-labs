
##########################################
# SECURITY GROUP
##########################################

variable "vpc_id" {
  type        = string
  description = "The ID of the VPC where the security group will be created"
}

variable "vpc_cidr_block" {
  type        = string
  description = "VPC CIDR allowed to reach the server (SSH / database ports / ping)"
}

variable "db_ports" {
  type        = list(number)
  description = "TCP ports of the database, open to the VPC, e.g. [5432] for PostgreSQL or [3306] for MySQL"
}

variable "sec_group_name" {
  type        = string
  description = "Name of the server's security group (unique in the VPC)"
}

variable "sec_group_description" {
  type        = string
  description = "Description of the server's security group"
  default     = "Allow SSH, the database ports and ping from the VPC"
}


##########################################
# EC2 INSTANCE
##########################################

variable "name" {
  type        = string
  description = "Name tag of the server, e.g. postgresql-source; its volumes are tagged <name>-data and <name>-backup"
}

variable "ami_id" {
  description = "AMI ID to launch the EC2 instance"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.small"
}

variable "cpu_credits" {
  description = "Burstable types (t3...) only. \"unlimited\": never throttled, CPU above the baseline is billed. \"standard\": fixed price, throttled when the CPU credits run out"
  type        = string
  default     = "unlimited"
}

variable "subnet_id" {
  description = "Subnet ID to launch the EC2 instance in"
  type        = string
}

variable "key_name" {
  description = "Key pair name for SSH access"
  type        = string
}

variable "iam_instance_profile" {
  description = "Instance profile (IAM role) of the server, e.g. its access to the backup bucket. null: none"
  type        = string
  default     = null
}

variable "volume_size" {
  description = "Root (OS) EBS Volume GBs Size"
  type        = number
  default     = 10
}

variable "volume_type" {
  description = "Root (OS) EBS Volume Type"
  type        = string
  default     = "gp3"
}


##########################################
# DATA AND BACKUP VOLUMES
##########################################

variable "data_volume_size" {
  description = "Data EBS volume size in GB (the database's files)"
  type        = number
  default     = 20
}

variable "data_volume_iops" {
  description = "Data volume IOPS (gp3: 3000 included in the price)"
  type        = number
  default     = 3000
}

variable "data_volume_throughput" {
  description = "Data volume throughput in MB/s (gp3: 125 included in the price)"
  type        = number
  default     = 125
}

variable "backup_volume_size" {
  description = "Backup EBS volume size in GB (local copy of the backups before S3). 0: no backup volume"
  type        = number
  default     = 0
}
