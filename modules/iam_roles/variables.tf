variable "name" {
  description = "Name of the IAM role and its instance profile, e.g. postgresql-single-db-server. Unique in the AWS account."
  type        = string
}

variable "backup_bucket_arn" {
  description = "ARN of the backup bucket the server may use."
  type        = string
}

variable "backup_prefixes" {
  description = "Folders of the bucket the server may use (no slashes), e.g. [\"pgbackrest\", \"pg_dump\"]. pgBackRest keeps one repository per cluster below its folder."
  type        = list(string)
  default     = ["pgbackrest"]
}

variable "s3_endpoint_id" {
  description = "S3 gateway endpoint of the VPC: the role may reach the bucket only through it, i.e. only from the VPC."
  type        = string
}
