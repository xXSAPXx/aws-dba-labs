output "bucket_name" {
  description = "Name of the backup bucket."
  value       = aws_s3_bucket.backups.id
}

output "bucket_arn" {
  description = "ARN of the backup bucket (for IAM policies)."
  value       = aws_s3_bucket.backups.arn
}

output "bucket_region" {
  description = "Region of the backup bucket."
  value       = aws_s3_bucket.backups.region
}
