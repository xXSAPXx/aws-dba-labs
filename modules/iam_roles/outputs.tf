output "role_name" {
  description = "Name of the IAM role."
  value       = aws_iam_role.server.name
}

output "instance_profile_name" {
  description = "Name of the instance profile, for the EC2 instance's iam_instance_profile."
  value       = aws_iam_instance_profile.server.name
}
