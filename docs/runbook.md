# Runbook

All commands run from the repo root. Set the names first:

```bash
export AWS_REGION=ap-south-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
Q=https://sqs.$AWS_REGION.amazonaws.com/$ACCOUNT
```

## Start and stop

Apply the state bucket first, then the main stack. Copy `infra/aws/terraform.tfvars.example` to
`terraform.tfvars` and fill it in.

```bash
terraform -chdir=infra/aws/bootstrap init
terraform -chdir=infra/aws/bootstrap apply
terraform -chdir=infra/aws init -backend-config="bucket=ops-agent-tfstate-$ACCOUNT"
terraform -chdir=infra/aws apply
```

Copy the outputs into `.env` (`OPS_API_URL`, `OPS_WORK_QUEUE_URL`, `OPS_RUNS_TABLE`), then set up
the worker profile once:

```bash
aws configure set profile.ops-worker.role_arn "$(terraform -chdir=infra/aws output -raw worker_role_arn)"
aws configure set profile.ops-worker.source_profile default
aws configure set profile.ops-worker.region $AWS_REGION
```

Run the worker. It needs Postgres, ingested policies and the mock API.

```bash
AWS_PROFILE=ops-worker uv run python -m integrations.queue_worker
```

Destroy after every session, main stack first:

```bash
terraform -chdir=infra/aws destroy
terraform -chdir=infra/aws/bootstrap destroy
```

## DLQ full

An alarm email names the queue. Look at the message first.

```bash
aws sqs receive-message --queue-url "$Q/ops-agent-work-dlq" --attribute-names All
```

- `intake-dlq`: the message failed validation or AWS was unavailable. Check the Lambda log group
  `/aws/lambda/ops-agent-intake`. A malformed request should be fixed and sent again by its sender.
- `work-dlq`: the worker failed 3 times. Check its output and Langfuse. Common causes are Postgres
  or the mock API being down, and a spent LLM cap.
- The state machine stops waiting after 15 minutes and marks the request `failed`, so a redriven
  message cannot finish that run. Resend the request under a new `id`.
- Delete the message once it is dealt with: `aws sqs purge-queue --queue-url "$Q/ops-agent-work-dlq"`.

## Spend cap hit

- One run: `BudgetExceeded` is raised and the message goes through retries to `work-dlq`. Find out
  why the run used so much in Langfuse before raising `LLM_MAX_SPEND_PER_RUN_USD`.
- Monthly AWS budget email: run `terraform destroy` first, then check AWS Billing.
- Stop intake without destroying: `aws events disable-rule --name ops-agent-jira-poll`, and stop
  the worker.

## Stuck approval

See what is waiting:

```bash
aws dynamodb scan --table-name ops-agent-runs --filter-expression "#s = :s" \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values '{":s":{"S":"pending_approval"}}' \
  --projection-expression request_id,run_id
```

Decide:

```bash
AWS_PROFILE=ops-worker uv run python -m integrations.queue_worker --approve REQ-1 --decision approve --by NAME
```

- Decisions are `approve`, `reject` or `edit` (with `--args`).
- The worker must be running to carry out the decision.
- The state machine gives up after 3 days and marks the request `failed`. Nothing was executed, so
  answer the customer by hand or resend the request under a new `id`.

## Key rotation

- Jira: create a new token at id.atlassian.com, put it in `.env` and `terraform.tfvars`, run
  `terraform -chdir=infra/aws apply`, then revoke the old token.
- Anthropic: create a new key, update `.env` and the `ANTHROPIC_API_KEY` repository secret, then
  revoke the old key.
- Langfuse: create new keys in project settings, update `.env`, delete the old pair.
- AWS: create a new access key for the IAM user, run `aws configure`, confirm with
  `aws sts get-caller-identity`, then delete the old key. The worker role has no long-lived keys.
- Postgres `sql_reader`: change `SQL_READER_PASSWORD` in `.env` and run
  `uv run python -m agent.run_history setup`.
