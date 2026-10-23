
##########################################
# PMM SECURITY GROUP VARIABLES
##########################################

variable "vpc_id" {
  type        = string
  description = "The ID of the VPC where the security group will be created"
}

variable "vpc_cidr_block" {
  type        = string
  description = "VPC CIDR: PMM Clients inside the VPC connect to the PMM Server on 443"
}

variable "admin_cidr" {
  type        = string
  description = "Your public IP in CIDR form. Only this range can reach SSH and the PMM UI"
}

variable "extra_admin_ports" {
  type        = list(number)
  description = "More TCP ports open to admin_cidr only, e.g. [3000, 9090] for Grafana and Prometheus next to PMM"
  default     = []
}

variable "sec_group_name" {
  description = "Name for the PMM Server Security Group"
  type        = string
  default     = "PMM_Server_SG"
}

variable "sec_group_description" {
  description = "Description for the PMM Server Security Group"
  type        = string
  default     = "Allow SSH / PMM Ports"
}


##########################################
# PMM EC2 INSTANCE VARIABLES
##########################################

variable "ami_id" {
  description = "AMI ID to launch the EC2 instance"
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type (PMM needs at least 2 GB RAM)"
  type        = string
  default     = "t3.small"
}

variable "subnet_id" {
  description = "Public Subnet ID to launch the EC2 instance in"
  type        = string
}

variable "key_name" {
  description = "Key pair name for SSH access"
  type        = string
}

variable "iam_instance_profile" {
  description = "Instance profile (IAM role) of the server, e.g. for Prometheus's EC2 service discovery. null: none"
  type        = string
  default     = null
}

variable "volume_size" {
  description = "EBS Volume GBs Size"
  type        = number
  default     = 10
}

variable "volume_type" {
  description = "EBS Volume Type"
  type        = string
  default     = "gp3"
}

variable "pmm_tag_name" {
  description = "Tags to apply to the EC2 instance"
  type        = string
  default     = "pmm-server"
}

variable "cpu_credits" {
  description = "Burstable types (t3...) only. \"unlimited\": never throttled, CPU above the baseline is billed. \"standard\": fixed price, throttled when the CPU credits run out"
  type        = string
  default     = "unlimited"
}
