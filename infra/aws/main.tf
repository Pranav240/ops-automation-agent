terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }
  # bucket comes from: terraform init -backend-config="bucket=<state_bucket output>"
  backend "s3" {
    key          = "ops-agent/terraform.tfstate"
    region       = "ap-south-1"
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region
  default_tags {
    tags = { project = "ops-agent" }
  }
}

data "aws_caller_identity" "current" {}

locals {
  name    = "ops-agent"
  account = data.aws_caller_identity.current.account_id
}
