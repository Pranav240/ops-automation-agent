#!/usr/bin/env bash
# Remove the AWS stack safely. Run after every session.
#
# A run paused for approval blocks the state machine from being deleted, so stop running
# executions first. The state bucket goes last, and only if the main stack really is gone:
# without its state, Terraform cannot clean up anything left behind.
set -euo pipefail

REGION=ap-south-1
# tr: the AWS CLI on Windows ends lines with \r, which would corrupt the names below.
ACCOUNT=$(aws sts get-caller-identity --query Account --output text | tr -d '\r')
MACHINE=arn:aws:states:$REGION:$ACCOUNT:stateMachine:ops-agent-request

running=$(aws stepfunctions list-executions --region "$REGION" --state-machine-arn "$MACHINE" \
  --status-filter RUNNING --query "executions[].executionArn" --output text 2>/dev/null | tr -d '\r' || true)
for execution in $running; do
  aws stepfunctions stop-execution --region "$REGION" --execution-arn "$execution" >/dev/null
  echo "stopped $execution"
done

# The EKS stack goes first, so nothing keeps charging if a later step stops.
eks=infra/aws/eks
if [ -d "$eks/.terraform" ]; then
  terraform -chdir="$eks" destroy -auto-approve -input=false -var admin_cidr=0.0.0.0/32
  left=$(terraform -chdir="$eks" state list)
  if [ -n "$left" ]; then
    echo "EKS stack still has resources, stopping here:" >&2
    echo "$left" >&2
    exit 1
  fi
fi

terraform -chdir=infra/aws destroy -auto-approve -input=false

left=$(terraform -chdir=infra/aws state list)
if [ -n "$left" ]; then
  echo "main stack still has resources, keeping the state bucket:" >&2
  echo "$left" >&2
  exit 1
fi

terraform -chdir=infra/aws/bootstrap destroy -auto-approve -input=false
echo "all destroyed"
