# POST /requests (signed, throttled) -> intake queue -> intake Lambda -> Step Functions.
# Every 5 minutes EventBridge runs the Jira poll Lambda, which also feeds the intake queue.

resource "aws_sqs_queue" "intake_dlq" {
  name                      = "${local.name}-intake-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "intake" {
  name                       = "${local.name}-intake"
  visibility_timeout_seconds = 120 # at least 6 times the Lambda timeout
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.intake_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_apigatewayv2_api" "intake" {
  name          = local.name
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_integration" "intake" {
  api_id              = aws_apigatewayv2_api.intake.id
  integration_type    = "AWS_PROXY"
  integration_subtype = "SQS-SendMessage"
  credentials_arn     = aws_iam_role.api.arn
  request_parameters = {
    QueueUrl    = aws_sqs_queue.intake.url
    MessageBody = "$request.body"
  }
}

resource "aws_apigatewayv2_route" "requests" {
  api_id             = aws_apigatewayv2_api.intake.id
  route_key          = "POST /requests"
  target             = "integrations/${aws_apigatewayv2_integration.intake.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.intake.id
  name        = "$default"
  auto_deploy = true
  default_route_settings {
    throttling_burst_limit = 5
    throttling_rate_limit  = 2
  }
}

data "archive_file" "intake" {
  type        = "zip"
  source_file = "${path.module}/lambdas/intake/handler.py"
  output_path = "${path.module}/.build/intake.zip"
}

data "archive_file" "jira_poll" {
  type        = "zip"
  source_file = "${path.module}/lambdas/jira_poll/handler.py"
  output_path = "${path.module}/.build/jira_poll.zip"
}

resource "aws_cloudwatch_log_group" "intake" {
  name              = "/aws/lambda/${local.name}-intake"
  retention_in_days = 3
}

resource "aws_cloudwatch_log_group" "jira_poll" {
  name              = "/aws/lambda/${local.name}-jira-poll"
  retention_in_days = 3
}

resource "aws_lambda_function" "intake" {
  function_name    = "${local.name}-intake"
  role             = aws_iam_role.intake.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.intake.output_path
  source_code_hash = data.archive_file.intake.output_base64sha256
  timeout          = 15
  memory_size      = 128
  environment {
    variables = {
      RUNS_TABLE         = aws_dynamodb_table.runs.name
      ATTACHMENTS_BUCKET = aws_s3_bucket.attachments.bucket
      STATE_MACHINE_ARN  = aws_sfn_state_machine.request.arn
    }
  }
  depends_on = [aws_cloudwatch_log_group.intake]
}

resource "aws_lambda_event_source_mapping" "intake" {
  event_source_arn = aws_sqs_queue.intake.arn
  function_name    = aws_lambda_function.intake.arn
  batch_size       = 1
}

resource "aws_lambda_function" "jira_poll" {
  function_name    = "${local.name}-jira-poll"
  role             = aws_iam_role.jira_poll.arn
  runtime          = "python3.13"
  architectures    = ["arm64"]
  handler          = "handler.handler"
  filename         = data.archive_file.jira_poll.output_path
  source_code_hash = data.archive_file.jira_poll.output_base64sha256
  timeout          = 60
  memory_size      = 128
  environment {
    variables = {
      INTAKE_QUEUE_URL = aws_sqs_queue.intake.url
      JIRA_BASE_URL    = var.jira_base_url
      JIRA_EMAIL       = var.jira_email
      JIRA_PROJECT_KEY = var.jira_project_key
      JIRA_TOKEN_PARAM = aws_ssm_parameter.jira_token.name
    }
  }
  depends_on = [aws_cloudwatch_log_group.jira_poll]
}

resource "aws_ssm_parameter" "jira_token" {
  name  = "/${local.name}/jira-api-token"
  type  = "SecureString"
  value = var.jira_api_token
}

resource "aws_cloudwatch_event_rule" "jira_poll" {
  name                = "${local.name}-jira-poll"
  schedule_expression = "rate(5 minutes)"
}

resource "aws_cloudwatch_event_target" "jira_poll" {
  rule = aws_cloudwatch_event_rule.jira_poll.name
  arn  = aws_lambda_function.jira_poll.arn
}

resource "aws_lambda_permission" "jira_poll" {
  statement_id  = "AllowEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.jira_poll.function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.jira_poll.arn
}
