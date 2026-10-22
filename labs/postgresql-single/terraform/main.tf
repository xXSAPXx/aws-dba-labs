

####################################################################################################################################
######################################## LAB: postgresql-single ####################################################################
# Single Percona PostgreSQL 17 server (private subnet) monitored by PMM 3 (public subnet, also the SSH jump host).
# Terraform builds the infrastructure, Ansible (../ansible) configures the servers.
# The modules are shared with the other labs: ../../../modules.


# Server image: the newest official Rocky Linux 10 (free, RHEL-compatible), published by the Rocky Enterprise Software Foundation.
# The instances ignore later image updates, so a new Rocky release never replaces the servers of a running lab.
######################################################################################

data "aws_ami" "rocky_10" {
  most_recent = true
  owners      = ["792107900819"]

  filter {
    name   = "name"
    values = ["Rocky-10-EC2-Base-10.*"]
  }

  filter {
    name   = "architecture"
    values = ["x86_64"]
  }
}

locals {
  ssh_user = "rocky" # default user of the Rocky Linux images
}


# Networking:
# Create VPC / Subnets / Nat_Gateway / Routing / Internet_Gateway
######################################################################################

module "vpc" {
  source = "../../../modules/vpc"

  # --- General VPC Settings ---
  name           = "postgresql-single"
  vpc_cidr_block = "10.0.0.0/24"

  # --- Availability Zone Settings ---
  availability_zone_1 = "us-east-1a"
  availability_zone_2 = "us-east-1b"


  # --- Subnet CIDR Block Settings ---
  public_subnet_1_cidr  = "10.0.0.0/28"
  private_subnet_1_cidr = "10.0.0.32/28"
  private_subnet_2_cidr = "10.0.0.48/28"

}


# Backups: S3 bucket for pgBackRest, and the database server's IAM role
# (one bucket per lab, destroyed with it; the server reaches it only through the VPC's S3 endpoint)
######################################################################################

data "aws_caller_identity" "current" {}

module "backup_bucket" {
  source = "../../../modules/s3_for_backups"

  bucket_name = "aws-postgresql-lab-postgresql-single-${data.aws_caller_identity.current.account_id}"
}

module "postgresql_role" {
  source = "../../../modules/iam_roles"

  name              = "postgresql-single-db-server"
  backup_bucket_arn = module.backup_bucket.bucket_arn
  backup_prefix     = "pgbackrest"
  s3_endpoint_id    = module.vpc.s3_endpoint_id
}


# Create the EC2: (PostgreSQL Database)
######################################################################################
module "postgresql_ec2_instance" {
  source = "../../../modules/db_server"

  # --- Security group: SSH, PostgreSQL and ping from the VPC ---
  vpc_id                = module.vpc.vpc_id
  vpc_cidr_block        = module.vpc.vpc_cidr_block
  db_ports              = [5432]
  sec_group_name        = "PostgreSQL_EC2_Instance_SG"
  sec_group_description = "Allow SSH / PMM and PostgreSQL Ports"

  # --- Server ---
  name          = "postgresql-source"
  ami_id        = data.aws_ami.rocky_10.id
  instance_type = var.postgresql_instance_type
  cpu_credits   = var.cpu_credits
  key_name      = var.aws_key_pair
  subnet_id     = module.vpc.private_subnet_1_id
  # Its IAM role: access to the backup bucket (attached and changed without replacing the server)
  iam_instance_profile = module.postgresql_role.instance_profile_name

  # EBS Volume Settings (root = OS; data = PostgreSQL, mounted at /var/lib/pgsql):
  volume_size      = 10
  volume_type      = "gp3"
  data_volume_size = var.postgresql_data_volume_size
}


# Create the EC2: (PMM Server + SSH jump host)
######################################################################################
module "pmm_server" {
  source = "../../../modules/pmm_server"

  # --- PMM_EC2_Instance Sec_Group Settings ---
  vpc_id                = module.vpc.vpc_id
  vpc_cidr_block        = module.vpc.vpc_cidr_block
  admin_cidr            = var.admin_cidr # Only your IP can reach SSH and the PMM UI.
  sec_group_name        = "PMM_Server_SG"
  sec_group_description = "Allow SSH / PMM Ports"

  # --- PMM_EC2_Instance Settings ---
  ami_id        = data.aws_ami.rocky_10.id
  instance_type = var.pmm_instance_type
  cpu_credits   = var.cpu_credits
  key_name      = var.aws_key_pair
  subnet_id     = module.vpc.public_subnet_1_id
  #iam_instance_profile         = module.iam_roles............
  pmm_tag_name = "pmm-server"

  # EBS Volume Settings:
  volume_size = var.pmm_volume_size
  volume_type = "gp3"
}
