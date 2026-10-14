
##########################################
# POSTGRESQL SECURITY GROUP VARIABLES
##########################################

variable "vpc_id" {
  type        = string
  description = "The ID of the VPC where the security group will be created"
}

variable "vpc_cidr_block" {
  type        = string
  description = "VPC CIDR allowed to reach the instance (SSH / PostgreSQL / ping)"
}

variable "sec_group_name" {
  description = "Name for the PostgreSQL Instance Security Group"
  type        = string
  default     = "PostgreSQL_EC2_Instance_SG"
}

variable "sec_group_description" {
  description = "Description for the PostgreSQL Instance Security Group"
  type        = string
  default     = "Allow SSH / PMM and PostgreSQL Ports"
}


##########################################
# POSTGRESQL EC2 INSTANCE VARIABLES
##########################################

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

variable "data_volume_size" {
  description = "Data EBS volume size in GB (PostgreSQL data, mounted at /var/lib/pgsql)"
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

variable "postgresql_tag_name" {
  description = "Tags to apply to the EC2 instance"
  type        = string
  default     = "postgresql-source"
}
