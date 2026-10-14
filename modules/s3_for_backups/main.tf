###################################################################################
# S3 bucket for database backups (pgBackRest repository), one per lab.
# The backup tool decides what to keep (e.g. pgBackRest's retention). S3 never deletes current files by age:
# that would break the backup repository (its control files are written once and stay old, and newer
# backups reference files of older ones).
###################################################################################

data "aws_caller_identity" "current" {}

resource "aws_s3_bucket" "backups" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy
}

# The bucket owner owns every object; ACLs are off.
resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Encryption at rest with Amazon S3 managed keys (SSE-S3): nothing to manage, no KMS permissions needed.
resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Versioning: an overwritten or deleted file stays as an older version (see the lifecycle below).
resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Only older versions expire: whatever the backup tool deletes stays recoverable for noncurrent_version_days.
resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    id     = "old-versions-and-unfinished-uploads"
    status = "Enabled"
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_days
    }

    # Removes a deleted file's marker once its older versions are gone; never touches current files.
    expiration {
      expired_object_delete_marker = true
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_incomplete_upload_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.backups]
}

# Refuse requests without TLS, and principals outside this AWS account. No network condition here, so the
# account's admins keep access from anywhere (console, CLI, terraform destroy). The servers' own roles are
# limited to the VPC endpoint in their IAM policy (module iam_roles).
data "aws_iam_policy_document" "backups" {
  statement {
    sid       = "DenyWithoutTLS"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "DenyOtherAccounts"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.backups.arn, "${aws_s3_bucket.backups.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:PrincipalAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_s3_bucket_policy" "backups" {
  bucket = aws_s3_bucket.backups.id
  policy = data.aws_iam_policy_document.backups.json

  # A bucket policy can only be set once public access is blocked.
  depends_on = [aws_s3_bucket_public_access_block.backups]
}
