output "api_url" {
  value = aws_apigatewayv2_stage.default.invoke_url
}

output "work_queue_url" {
  value = aws_sqs_queue.work.url
}

output "runs_table" {
  value = aws_dynamodb_table.runs.name
}

output "worker_role_arn" {
  value = aws_iam_role.worker.arn
}

output "dashboard_role_arn" {
  value = aws_iam_role.dashboard.arn
}

output "state_machine_arn" {
  value = aws_sfn_state_machine.request.arn
}
