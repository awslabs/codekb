variable "region" {
  type    = string
  default = "us-east-1"
}

variable "bucket_prefix" {
  type    = string
  default = "logs"
}

locals {
  bucket_name = "${var.bucket_prefix}-${var.region}"
  common_tags = {
    Environment = "prod"
  }
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
}

resource "aws_s3_bucket" "logs" {
  bucket = local.bucket_name
  region = var.region
  tags   = local.common_tags
}

resource "aws_instance" "web" {
  ami           = data.aws_ami.ubuntu.id
  instance_type = "t3.micro"
  subnet_id     = module.vpc.subnet_ids[0]
  log_bucket    = aws_s3_bucket.logs.arn

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    encrypted = true
  }

  ingress {
    cidr_blocks = [var.region]
  }
}

module "vpc" {
  source = "./modules/vpc"
  region = var.region
}

output "instance_ip" {
  value = aws_instance.web.private_ip
}
