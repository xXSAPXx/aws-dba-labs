
# Configure Terraform Remote Backend (one state file per lab):
# The bucket must exist before `terraform init`. To use your own bucket:
#   terraform init -backend-config="bucket=<your-bucket>"
terraform {
  backend "s3" {
    bucket       = "terraform-postgresql-playground-tfstate"
    key          = "labs/mysql-single/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}
