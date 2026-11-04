# State machine, work queue and storage. The worker takes jobs from the work queue and returns
# each result with the job's task token. A risky run waits at WaitForApproval: its token is stored
# in DynamoDB, where the approval command reads it.

resource "aws_sqs_queue" "work_dlq" {
  name                      = "${local.name}-work-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "work" {
  name                       = "${local.name}-work"
  visibility_timeout_seconds = 180 # the agent's p95 is 20 s
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.work_dlq.arn
    maxReceiveCount     = 3
  })
}

resource "aws_dynamodb_table" "runs" {
  name         = "${local.name}-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "request_id"
  attribute {
    name = "request_id"
    type = "S"
  }
}

resource "aws_s3_bucket" "attachments" {
  bucket        = "${local.name}-attachments-${local.account}"
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "attachments" {
  bucket                  = aws_s3_bucket.attachments.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_lifecycle_configuration" "attachments" {
  bucket = aws_s3_bucket.attachments.id
  rule {
    id     = "expire"
    status = "Enabled"
    filter {}
    expiration {
      days = 7
    }
  }
}

locals {
  key = { request_id = { "S.$" = "$.request.id" } }

  record_status = {
    Type     = "Task"
    Resource = "arn:aws:states:::dynamodb:updateItem"
    Parameters = {
      TableName                = aws_dynamodb_table.runs.name
      Key                      = local.key
      UpdateExpression         = "SET #s = :s REMOVE approval_token"
      ExpressionAttributeNames = { "#s" = "status" }
      ExpressionAttributeValues = {
        ":s" = { "S.$" = "$.result.status" }
      }
    }
    ResultPath = null
    End        = true
  }

  mark_failed = {
    Type     = "Task"
    Resource = "arn:aws:states:::dynamodb:updateItem"
    Parameters = {
      TableName                 = aws_dynamodb_table.runs.name
      Key                       = local.key
      UpdateExpression          = "SET #s = :s REMOVE approval_token"
      ExpressionAttributeNames  = { "#s" = "status" }
      ExpressionAttributeValues = { ":s" = { S = "failed" } }
    }
    ResultPath = null
    Next       = "Failed"
  }

  catch_all = [{ ErrorEquals = ["States.ALL"], ResultPath = "$.error", Next = "MarkFailed" }]

  definition = {
    Comment = "One request: run the agent, wait for a human if it is risky, then finish."
    StartAt = "RunAgent"
    States = {
      RunAgent = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage.waitForTaskToken"
        Parameters = {
          QueueUrl = aws_sqs_queue.work.url
          MessageBody = {
            type           = "run"
            "request.$"    = "$.request"
            "task_token.$" = "$$.Task.Token"
          }
        }
        TimeoutSeconds = 900
        ResultPath     = "$.result"
        Catch          = local.catch_all
        Next           = "NeedsApproval"
      }
      NeedsApproval = {
        Type = "Choice"
        Choices = [{
          Variable     = "$.result.status"
          StringEquals = "pending_approval"
          Next         = "WaitForApproval"
        }]
        Default = "Record"
      }
      WaitForApproval = {
        Type     = "Task"
        Resource = "arn:aws:states:::aws-sdk:dynamodb:updateItem.waitForTaskToken"
        Parameters = {
          TableName                = aws_dynamodb_table.runs.name
          Key                      = local.key
          UpdateExpression         = "SET #s = :s, approval_token = :t"
          ExpressionAttributeNames = { "#s" = "status" }
          ExpressionAttributeValues = {
            ":s" = { S = "pending_approval" }
            ":t" = { "S.$" = "$$.Task.Token" }
          }
        }
        TimeoutSeconds = 259200
        ResultPath     = "$.approval"
        Catch          = local.catch_all
        Next           = "ResumeAgent"
      }
      ResumeAgent = {
        Type     = "Task"
        Resource = "arn:aws:states:::sqs:sendMessage.waitForTaskToken"
        Parameters = {
          QueueUrl = aws_sqs_queue.work.url
          MessageBody = {
            type           = "resume"
            "request.$"    = "$.request"
            "answer.$"     = "$.approval"
            "task_token.$" = "$$.Task.Token"
          }
        }
        TimeoutSeconds = 900
        ResultPath     = "$.result"
        Catch          = local.catch_all
        Next           = "Record"
      }
      Record     = local.record_status
      MarkFailed = local.mark_failed
      Failed     = { Type = "Fail", Error = "RunFailed" }
    }
  }
}

resource "aws_cloudwatch_log_group" "state_machine" {
  name              = "/aws/vendedlogs/states/${local.name}"
  retention_in_days = 3
}

resource "aws_sfn_state_machine" "request" {
  name       = "${local.name}-request"
  type       = "STANDARD"
  role_arn   = aws_iam_role.state_machine.arn
  definition = jsonencode(local.definition)
  logging_configuration {
    log_destination = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    level           = "ERROR"
  }
}
