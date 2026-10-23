
# Required Terraform Providers and Versions:
terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    # Writes the SSH config and the Ansible inventory on your machine:
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }
}


# AWS provider region + tags added to every resource of this lab
# (use the "Lab" tag to find leftover resources and track cost per lab):
provider "aws" {
  region = "us-east-1"

  default_tags {
    tags = {
      Project   = "aws-dba-labs"
      Lab       = "mysql-single"
      ManagedBy = "terraform"
    }
  }
}
