variable "bucket_name" {
  description = "Name of the backup bucket. Globally unique: include the account ID, e.g. aws-postgresql-lab-<lab>-<account id>."
  type        = string
}

variable "force_destroy" {
  description = "true: terraform destroy deletes every object and every version, then the bucket. For labs; never for production backups."
  type        = bool
  default     = true
}

variable "noncurrent_version_days" {
  description = "Days an overwritten or deleted object stays recoverable as an older version before S3 removes it."
  type        = number
  default     = 7
}

variable "abort_incomplete_upload_days" {
  description = "Days after which S3 aborts multipart uploads that never finished (e.g. an interrupted backup), so their parts stop costing."
  type        = number
  default     = 1
}
