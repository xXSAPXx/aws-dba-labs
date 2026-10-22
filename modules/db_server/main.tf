###################################################################################
# Database server: EC2 instance in a private subnet, its security group, its data volume and,
# optionally, a backup volume. Used by every lab (PostgreSQL, MySQL, ...).
# The server is configured by Ansible (labs/<lab>/ansible), not by user_data.
###################################################################################


########################################################################
# Security group: SSH, the database ports and ping, only from inside the VPC.
########################################################################

resource "aws_security_group" "this" {
  name        = var.sec_group_name
  description = var.sec_group_description
  vpc_id      = var.vpc_id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  dynamic "ingress" {
    for_each = var.db_ports
    content {
      description = "Database"
      from_port   = ingress.value
      to_port     = ingress.value
      protocol    = "tcp"
      cidr_blocks = [var.vpc_cidr_block]
    }
  }

  ingress {
    description = "Ping"
    from_port   = -1
    to_port     = -1
    protocol    = "icmp"
    cidr_blocks = [var.vpc_cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}


########################################################################
# The server:
########################################################################

resource "aws_instance" "this" {
  ami                    = var.ami_id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.this.id]
  key_name               = var.key_name
  iam_instance_profile   = var.iam_instance_profile

  # Burstable types (t3...): "unlimited" never throttles and bills CPU above the baseline, "standard"
  # throttles when the CPU credits run out (a t3 starts with none). Other types don't take this setting.
  dynamic "credit_specification" {
    for_each = startswith(var.instance_type, "t") ? [1] : []
    content {
      cpu_credits = var.cpu_credits
    }
  }

  root_block_device {
    volume_size = var.volume_size
    volume_type = var.volume_type
    encrypted   = true
  }

  tags = {
    Name = var.name
  }

  # A newer image (e.g. from an "always the latest" AMI lookup) must not replace a running server:
  lifecycle {
    ignore_changes = [ami]
  }
}


########################################################################
# Data volume: the database's files on their own EBS disk (Ansible mounts it, e.g. at /var/lib/pgsql).
# Size, IOPS and throughput change in place (no new disk), e.g. to grow a full disk.
########################################################################

data "aws_subnet" "this" {
  id = var.subnet_id
}

resource "aws_ebs_volume" "data" {
  availability_zone = data.aws_subnet.this.availability_zone
  size              = var.data_volume_size
  type              = "gp3"
  iops              = var.data_volume_iops
  throughput        = var.data_volume_throughput
  encrypted         = true

  tags = {
    Name = "${var.name}-data"
  }
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.this.id

  # On destroy, stop the server first: a mounted disk can't be detached cleanly from a running one.
  stop_instance_before_detaching = true
}


########################################################################
# Backup volume (optional, backup_volume_size > 0): a local copy of the backups before they go to S3
# (Ansible mounts it, e.g. at /backup). A separate disk, so a full backup disk never stops the database.
########################################################################

resource "aws_ebs_volume" "backup" {
  count = var.backup_volume_size > 0 ? 1 : 0

  availability_zone = data.aws_subnet.this.availability_zone
  size              = var.backup_volume_size
  type              = "gp3"
  encrypted         = true

  tags = {
    Name = "${var.name}-backup"
  }
}

resource "aws_volume_attachment" "backup" {
  count = var.backup_volume_size > 0 ? 1 : 0

  device_name = "/dev/sdg"
  volume_id   = aws_ebs_volume.backup[0].id
  instance_id = aws_instance.this.id

  stop_instance_before_detaching = true
}
