###################################################################################
# IAM role for a monitoring server (EC2 instance profile): read the account's list of EC2 instances,
# for Prometheus's EC2 service discovery (ec2_sd_configs). Read-only: it can't change anything.
# No keys on the server: Prometheus takes the role's temporary credentials from the instance metadata.
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

# The instances with their tags and addresses; the zones turn into the availability zone ID label.
# EC2's Describe calls don't support resource-level permissions, so the resource is "*".
data "aws_iam_policy_document" "discovery" {
  statement {
    sid       = "DescribeInstances"
    actions   = ["ec2:DescribeInstances", "ec2:DescribeAvailabilityZones"]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "discovery" {
  name   = "ec2-discovery"
  role   = aws_iam_role.server.id
  policy = data.aws_iam_policy_document.discovery.json
}
