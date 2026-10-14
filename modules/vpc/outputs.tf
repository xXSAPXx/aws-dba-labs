

output "vpc_id" {
  description = "The ID of the VPC."
  value       = aws_vpc.vpc.id
}

output "vpc_name" {
  description = "The name of the VPC."
  value       = aws_vpc.vpc.tags.Name
}

output "vpc_cidr_block" {
  description = "The CIDR block of the VPC."
  value       = aws_vpc.vpc.cidr_block
}

output "public_subnet_1_id" {
  description = "The ID of the public subnet."
  value       = aws_subnet.public_subnet_1.id
}

output "private_subnet_1_id" {
  description = "The ID of private subnet 1 (availability_zone_1)."
  value       = aws_subnet.private_subnet_1.id
}

output "private_subnet_2_id" {
  description = "The ID of private subnet 2 (availability_zone_2)."
  value       = aws_subnet.private_subnet_2.id
}

output "s3_endpoint_id" {
  description = "The ID of the S3 gateway endpoint (private subnets). IAM policies can require it (aws:SourceVpce)."
  value       = aws_vpc_endpoint.s3.id
}
