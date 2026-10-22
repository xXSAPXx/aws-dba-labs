###################################################################################
# IAM role for a database server (EC2 instance profile): its backups in the S3 bucket.
# No keys on the server: the AWS CLI and SDKs, pgBackRest (repo1-s3-key-type=auto) and XtraBackup's xbcloud
# take the role's temporary credentials from the instance metadata.
###################################################################################

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "server" {
  name               = var.name
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_instance_profile" "server" {
  name = var.name
  role = aws_iam_role.server.name
}

# Backups: list, read, write and delete (the backup tool's retention deletes expired backups, e.g. pgBackRest)
# in the backup folder only, and only through the VPC's S3 endpoint.
data "aws_iam_policy_document" "backups" {
  statement {
    sid       = "ListTheBackupFolder"
    actions   = ["s3:ListBucket"]
    resources = [var.backup_bucket_arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = [var.backup_prefix, "${var.backup_prefix}/*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceVpce"
      values   = [var.s3_endpoint_id]
    }
  }

  statement {
    sid       = "ReadWriteDeleteBackups"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"]
    resources = ["${var.backup_bucket_arn}/${var.backup_prefix}/*"]
    condition {
      test     = "StringEquals"
      variable = "aws:SourceVpce"
      values   = [var.s3_endpoint_id]
    }
  }
}

resource "aws_iam_role_policy" "backups" {
  name   = "backups"
  role   = aws_iam_role.server.id
  policy = data.aws_iam_policy_document.backups.json
}
