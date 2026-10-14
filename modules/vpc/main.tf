

###################################################################################
# Create a VPC / 1_Public_Subnet and 2 Private Subnets NAT + Internet_Gateway
# Shared by all labs: every Name tag is prefixed with var.name (the lab name).
###################################################################################

# Create the VPC:
resource "aws_vpc" "vpc" {
  cidr_block           = var.vpc_cidr_block
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags = {
    Name = "${var.name}-vpc"
  }
}


# Create a Public_Subnet:
resource "aws_subnet" "public_subnet_1" {
  vpc_id                  = aws_vpc.vpc.id
  cidr_block              = var.public_subnet_1_cidr
  availability_zone       = var.availability_zone_1 # Replace with your preferred AZ
  map_public_ip_on_launch = true                    # Enable this to auto-assign public IPs
  tags = {
    Name = "${var.name}-public-subnet-1"
  }
}



# Create an internet gateway:
resource "aws_internet_gateway" "igw" {
  vpc_id = aws_vpc.vpc.id
  tags = {
    Name = "${var.name}-igw"
  }
}



##################################################################
# Create NAT GATEWAY in the public subnet:
##################################################################

# NAT Gateway in the public subnet:
resource "aws_eip" "nat_eip" {
  domain = "vpc"
  tags = {
    Name = "${var.name}-nat-eip"
  }
}

resource "aws_nat_gateway" "nat" {
  allocation_id = aws_eip.nat_eip.id


  # Create the NAT In the Public Subnet:
  subnet_id  = aws_subnet.public_subnet_1.id
  depends_on = [aws_internet_gateway.igw]

  tags = {
    Name = "${var.name}-nat"
  }
}


##################################################################
# Create 2 private_subnets for the VPC:
##################################################################


# Create a Private_Subnet_1:
resource "aws_subnet" "private_subnet_1" {
  vpc_id                  = aws_vpc.vpc.id
  cidr_block              = var.private_subnet_1_cidr # Make sure the CIDR block doesn't overlap with the public subnet
  availability_zone       = var.availability_zone_1   # Same AZ as the first public subnet
  map_public_ip_on_launch = false                     # DO NOT Auto-assign public IPs!
  tags = {
    Name = "${var.name}-private-subnet-1"
  }
}

# Create a Private_Subnet_2:
resource "aws_subnet" "private_subnet_2" {
  vpc_id                  = aws_vpc.vpc.id
  cidr_block              = var.private_subnet_2_cidr # Make sure the CIDR block doesn't overlap with the public subnet
  availability_zone       = var.availability_zone_2   # Second AZ (for replicas / HA nodes)
  map_public_ip_on_launch = false                     # DO NOT Auto-assign public IPs!
  tags = {
    Name = "${var.name}-private-subnet-2"
  }
}


##############################################################################
# Create Route_Tables for both public and private subnets:
##############################################################################


############### PUBLIC ROUTING TABLE and SUBNETS: ###############
# All traffic from the public subnet is going to the Internet Gateway:
resource "aws_route_table" "public_rt" {
  vpc_id = aws_vpc.vpc.id

  route {
    cidr_block = "0.0.0.0/0" # Any traffic destined for an address outside the VPC will be directed to the VPC internet gateway
    gateway_id = aws_internet_gateway.igw.id
  }

  tags = {
    Name = "${var.name}-public-rt"
  }
}

##### PUBLIC SUBNETS: #####
# Associate the route table with the public_subnet:
resource "aws_route_table_association" "public_rt_assoc" {
  subnet_id      = aws_subnet.public_subnet_1.id
  route_table_id = aws_route_table.public_rt.id
}


############### PRIVATE ROUTING TABLE and SUBNETS: ###############
# All traffic from the private subnets are going to the NAT Gateway:
resource "aws_route_table" "private_rt" {
  vpc_id = aws_vpc.vpc.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.nat.id
  }

  tags = {
    Name = "${var.name}-private-rt"
  }
}

##### PRIVATE SUBNETS: #####
# Associate the route table with the 1st PRIVATE subnet:
resource "aws_route_table_association" "private_rt_assoc" {
  subnet_id      = aws_subnet.private_subnet_1.id
  route_table_id = aws_route_table.private_rt.id
}

# Associate the route table with the 2nd PRIVATE subnet:
resource "aws_route_table_association" "private_rt_assoc_2" {
  subnet_id      = aws_subnet.private_subnet_2.id
  route_table_id = aws_route_table.private_rt.id
}


##############################################################################
# S3 gateway endpoint: S3 traffic from the private subnets (backups) goes straight to S3 instead of
# through the NAT gateway. The endpoint is free; the NAT gateway charges $0.045 per GB it passes.
##############################################################################

data "aws_region" "current" {}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.vpc.id
  service_name      = "com.amazonaws.${data.aws_region.current.name}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.private_rt.id]

  tags = {
    Name = "${var.name}-s3-endpoint"
  }
}


