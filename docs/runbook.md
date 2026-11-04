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

Run the worker. It needs Postgres, ingested policies and the mock API. The worker creates no
tables, so run `--setup` once first with your own AWS login.

```bash
uv run python -m integrations.queue_worker --setup
uv run uvicorn mock_api.app:app --env-file .env
AWS_PROFILE=ops-worker uv run python -m integrations.queue_worker
```

Destroy after every session. The script stops paused runs, which otherwise block the state
machine from being deleted, and keeps the state bucket unless the main stack really is gone:

```bash
bash scripts/destroy.sh
```

## Workers on kind

Scaling needs the AWS stack, because KEDA reads the real SQS queue.

```bash
kind create cluster --name ops-agent
docker build -f docker/mock-api.Dockerfile -t ops-agent-mock-api:local .
docker build -f docker/worker.Dockerfile -t ops-agent-worker:local --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) .
kind load docker-image ops-agent-mock-api:local ops-agent-worker:local --name ops-agent
helm repo add kedacore https://kedacore.github.io/charts
helm install keda kedacore/keda --version 2.21.0 --namespace keda --create-namespace --wait
uv run python scripts/helm_values.py --cluster kind
helm install ops-agent deploy/helm/ops-agent -f deploy/helm/values.local.yaml --wait --timeout 6m
uv run python scripts/scale_test.py --count 40 --cluster kind
```

- Temporary AWS keys last 1 hour. Run `helm_values.py` and `helm upgrade` again when they expire.
- Changing `POSTGRES_PASSWORD` needs a fresh install, because the database keeps its first password.
- Delete `deploy/helm/values.local.yaml` afterwards. It holds secrets and is gitignored.
- Finish with `kind delete cluster --name ops-agent`.

## Dashboard and injection test on kind

Do this on the kind cluster above. It also needs the AWS stack. Build the dashboard image before
`helm install`:

```bash
docker build -f docker/dashboard.Dockerfile -t ops-agent-dashboard:local .
kind load docker-image ops-agent-dashboard:local --name ops-agent
```

Open the dashboard, then sign in with `DASHBOARD_APPROVER_TOKEN` from `.env`:

```bash
kubectl port-forward deploy/ops-agent-dashboard 8090:8090
```

Attack the running system. This spends Claude API credits, about $0.05 a run. Use a new prefix each
time, because a request id is only ever handled once:

```bash
uv run python scripts/injection_test.py --prefix INJ-1
```

For a local look at the UI, run `npm run dev` in `dashboard/web`. It sends `/api` to port 8090.

## Workers on EKS

The dashboard stays off here. One proof run only. About $0.25 an hour on AWS, so destroy it the same day. Apply the main stack
first, then:

```bash
BUCKET=ops-agent-tfstate-$ACCOUNT
export TF_VAR_admin_cidr=$(curl -s https://checkip.amazonaws.com)/32
terraform -chdir=infra/aws/eks init -backend-config="bucket=$BUCKET"
terraform -chdir=infra/aws/eks apply
aws eks update-kubeconfig --name ops-agent --region $AWS_REGION --alias eks-ops-agent
REGISTRY=$(terraform -chdir=infra/aws/eks output -raw image_registry)
aws ecr get-login-password | docker login --username AWS --password-stdin $REGISTRY
TAG=$(git rev-parse --short HEAD)
docker tag ops-agent-worker:local $REGISTRY/ops-agent-worker:$TAG
docker tag ops-agent-mock-api:local $REGISTRY/ops-agent-mock-api:$TAG
docker push $REGISTRY/ops-agent-worker:$TAG
docker push $REGISTRY/ops-agent-mock-api:$TAG
helm install keda kedacore/keda --version 2.21.0 --namespace keda --create-namespace --wait \
  --set podIdentity.aws.irsa.enabled=true \
  --set podIdentity.aws.irsa.roleArn=$(terraform -chdir=infra/aws/eks output -raw keda_role_arn)
uv run python scripts/helm_values.py --cluster eks --commit $TAG
helm install ops-agent deploy/helm/ops-agent -f deploy/helm/values.local.yaml --wait --timeout 6m
uv run python scripts/scale_test.py --count 40 --cluster eks
```

Then run `bash scripts/destroy.sh`, delete `deploy/helm/values.local.yaml`, and check the bill the
next morning. Nodes do not scale in this proof, only pods.

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
uv run python -m integrations.queue_worker --approve REQ-1 --decision approve --by NAME
```

- Use your own AWS login. The worker role cannot read approval tokens. The dashboard is the normal
  way to decide.
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

## Bedrock access

Not working yet on this account. A real call returns 400 `Operation not allowed`.

- Check the state:
  `aws bedrock get-foundation-model-availability --model-id anthropic.claude-haiku-5-5 --region ap-south-1`
- Once the support case is resolved, submit the use case form and accept the terms in the Bedrock
  model catalog (Mumbai region).
- Then run the worker with `LLM_PROVIDER=bedrock` and `LLM_MODEL=global.anthropic.claude-haiku-5-5`.
