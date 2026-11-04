# One-day EKS proof run. Apply after the main stack, destroy with scripts/destroy.sh the same day.
# About $0.25 an hour. No NAT gateway: nodes sit in public subnets, and the only way in is the
# cluster's own security group.
terraform {
  required_version = ">= 1.10"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
  # bucket comes from: terraform init -backend-config="bucket=<state_bucket output>"
  backend "s3" {
    key          = "ops-agent/eks.tfstate"
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

variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "admin_cidr" {
  description = "Only this address can reach the cluster API from outside, e.g. 203.0.113.7/32."
  type        = string
}

variable "kubernetes_version" {
  description = "Must stay in standard support: extended support adds $0.50 an hour."
  type        = string
  default     = "1.36"
}

variable "node_type" {
  type    = string
  default = "t3.medium"
}

variable "node_count" {
  type    = number
  default = 2
}

data "aws_caller_identity" "current" {}

locals {
  name    = "ops-agent"
  account = data.aws_caller_identity.current.account_id
  prefix  = "arn:aws"
  oidc    = replace(aws_eks_cluster.main.identity[0].oidc[0].issuer, "https://", "")
  # The main stack owns these. Names, not lookups, so this stack can be planned on its own.
  work_queue_arn    = "${local.prefix}:sqs:${var.region}:${local.account}:${local.name}-work"
  state_machine_arn = "${local.prefix}:states:${var.region}:${local.account}:stateMachine:${local.name}-request"
  runs_table_arn    = "${local.prefix}:dynamodb:${var.region}:${local.account}:table/${local.name}-runs"
}

# Network
resource "aws_vpc" "main" {
  cidr_block           = "10.40.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true
  tags                 = { Name = local.name }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id
}

resource "aws_subnet" "public" {
  for_each                = { a = 0, b = 1 }
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.40.${each.value}.0/24"
  availability_zone       = "${var.region}${each.key}"
  map_public_ip_on_launch = true
  tags                    = { Name = "${local.name}-${each.key}" }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }
}

resource "aws_route_table_association" "public" {
  for_each       = aws_subnet.public
  subnet_id      = each.value.id
  route_table_id = aws_route_table.public.id
}

# Cluster
data "aws_iam_policy_document" "eks_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["eks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "cluster" {
  name               = "${local.name}-eks-cluster"
  assume_role_policy = data.aws_iam_policy_document.eks_assume.json
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKSClusterPolicy"
}

resource "aws_eks_cluster" "main" {
  name     = local.name
  role_arn = aws_iam_role.cluster.arn
  version  = var.kubernetes_version

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = true
  }

  upgrade_policy {
    support_type = "STANDARD"
  }

  vpc_config {
    subnet_ids              = [for subnet in aws_subnet.public : subnet.id]
    endpoint_public_access  = true
    endpoint_private_access = true # nodes reach the API inside the VPC
    public_access_cidrs     = [var.admin_cidr]
  }

  depends_on = [aws_iam_role_policy_attachment.cluster]
}

# Nodes
data "aws_iam_policy_document" "node_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "node" {
  name               = "${local.name}-eks-node"
  assume_role_policy = data.aws_iam_policy_document.node_assume.json
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/${each.value}"
}

resource "aws_eks_node_group" "main" {
  cluster_name    = aws_eks_cluster.main.name
  node_group_name = local.name
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = [for subnet in aws_subnet.public : subnet.id]
  instance_types  = [var.node_type]
  ami_type        = "AL2023_x86_64_STANDARD"
  capacity_type   = "ON_DEMAND" # spot could end a node during the proof
  disk_size       = 20

  # Pods scale, not nodes: both nodes run for the whole proof.
  scaling_config {
    desired_size = var.node_count
    min_size     = var.node_count
    max_size     = var.node_count
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# Images
resource "aws_ecr_repository" "image" {
  for_each     = toset(["worker", "mock-api"])
  name         = "${local.name}-${each.key}"
  force_delete = true
}

# Pod access to AWS. IRSA means no AWS key is stored in the cluster.
resource "aws_iam_openid_connect_provider" "cluster" {
  url            = aws_eks_cluster.main.identity[0].oidc[0].issuer
  client_id_list = ["sts.amazonaws.com"]
}

data "aws_iam_policy_document" "pod_assume" {
  for_each = {
    worker = "system:serviceaccount:default:ops-agent-worker"
    keda   = "system:serviceaccount:keda:keda-operator"
  }
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.cluster.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc}:sub"
      values   = [each.value]
    }
    condition {
      test     = "StringEquals"
      variable = "${local.oidc}:aud"
      values   = ["sts.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "pod" {
  for_each           = data.aws_iam_policy_document.pod_assume
  name               = "${local.name}-eks-${each.key}"
  assume_role_policy = each.value.json
}

# The same work as the Phase 5 worker role. Bedrock is left out until the account has access.
data "aws_iam_policy_document" "worker" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage"]
    resources = [local.work_queue_arn]
  }
  statement {
    actions   = ["states:SendTaskSuccess", "states:SendTaskFailure"]
    resources = [local.state_machine_arn]
  }
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [local.runs_table_arn]
  }
}

# KEDA only reads the queue depth.
data "aws_iam_policy_document" "keda" {
  statement {
    actions   = ["sqs:GetQueueAttributes"]
    resources = [local.work_queue_arn]
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.pod["worker"].id
  policy = data.aws_iam_policy_document.worker.json
}

resource "aws_iam_role_policy" "keda" {
  role   = aws_iam_role.pod["keda"].id
  policy = data.aws_iam_policy_document.keda.json
}

output "cluster_name" {
  value = aws_eks_cluster.main.name
}

output "worker_role_arn" {
  value = aws_iam_role.pod["worker"].arn
}

output "keda_role_arn" {
  value = aws_iam_role.pod["keda"].arn
}

output "image_registry" {
  value = "${local.account}.dkr.ecr.${var.region}.amazonaws.com"
}
