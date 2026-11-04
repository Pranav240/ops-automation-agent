# One role per piece, each with only what that piece does.

data "aws_iam_policy_document" "api_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["apigateway.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "states_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "worker_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "AWS"
      identifiers = [data.aws_caller_identity.current.arn]
    }
  }
}

# API Gateway: put messages on the intake queue.
resource "aws_iam_role" "api" {
  name               = "${local.name}-api"
  assume_role_policy = data.aws_iam_policy_document.api_assume.json
}

data "aws_iam_policy_document" "api" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.intake.arn]
  }
}

resource "aws_iam_role_policy" "api" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.api.json
}

# Intake Lambda.
resource "aws_iam_role" "intake" {
  name               = "${local.name}-intake"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "intake" {
  statement {
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.intake.arn]
  }
  statement {
    actions   = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.runs.arn]
  }
  statement {
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.attachments.arn}/requests/*"]
  }
  statement {
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.request.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.intake.arn}:*"]
  }
}

resource "aws_iam_role_policy" "intake" {
  role   = aws_iam_role.intake.id
  policy = data.aws_iam_policy_document.intake.json
}

# Jira poll Lambda.
resource "aws_iam_role" "jira_poll" {
  name               = "${local.name}-jira-poll"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "jira_poll" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.intake.arn]
  }
  statement {
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.jira_token.arn]
  }
  statement {
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.jira_poll.arn}:*"]
  }
}

resource "aws_iam_role_policy" "jira_poll" {
  role   = aws_iam_role.jira_poll.id
  policy = data.aws_iam_policy_document.jira_poll.json
}

# State machine.
resource "aws_iam_role" "state_machine" {
  name               = "${local.name}-state-machine"
  assume_role_policy = data.aws_iam_policy_document.states_assume.json
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.work.arn]
  }
  statement {
    actions   = ["dynamodb:UpdateItem"]
    resources = [aws_dynamodb_table.runs.arn]
  }
  statement {
    # Log delivery calls do not support resource-level permissions.
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "state_machine" {
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

# Worker. Assumed through the ops-worker profile on this PC, and with temporary keys on kind.
resource "aws_iam_role" "worker" {
  name               = "${local.name}-worker"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

data "aws_iam_policy_document" "worker" {
  statement {
    # GetQueueAttributes lets KEDA read the queue depth.
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.work.arn]
  }
  statement {
    actions   = ["states:SendTaskSuccess"]
    resources = [aws_sfn_state_machine.request.arn]
  }
  dynamic "statement" {
    for_each = var.bedrock_enabled ? [1] : []
    content {
      actions = ["bedrock:InvokeModel"]
      resources = [
        "arn:aws:bedrock:${var.region}:${local.account}:inference-profile/global.${var.bedrock_model}",
        "arn:aws:bedrock:${var.region}::foundation-model/${var.bedrock_model}",
        "arn:aws:bedrock:::foundation-model/${var.bedrock_model}",
      ]
    }
  }
}

resource "aws_iam_role_policy" "worker" {
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.worker.json
}

# Dashboard: read a run's approval token and send the human decision. Nothing else.
resource "aws_iam_role" "dashboard" {
  name               = "${local.name}-dashboard"
  assume_role_policy = data.aws_iam_policy_document.worker_assume.json
}

data "aws_iam_policy_document" "dashboard" {
  statement {
    actions   = ["dynamodb:GetItem"]
    resources = [aws_dynamodb_table.runs.arn]
  }
  statement {
    actions   = ["states:SendTaskSuccess"]
    resources = [aws_sfn_state_machine.request.arn]
  }
}

resource "aws_iam_role_policy" "dashboard" {
  role   = aws_iam_role.dashboard.id
  policy = data.aws_iam_policy_document.dashboard.json
}
