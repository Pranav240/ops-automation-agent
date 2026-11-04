variable "region" {
  type    = string
  default = "ap-south-1"
}

variable "budget_email" {
  description = "Gets the budget and alarm emails. Set it in terraform.tfvars (gitignored)."
  type        = string
}

variable "budget_usd" {
  description = "Monthly spending limit the budget alert watches."
  type        = number
  default     = 1
}

variable "jira_base_url" {
  type = string
}

variable "jira_email" {
  type = string
}

variable "jira_project_key" {
  type = string
}

variable "jira_api_token" {
  type      = string
  sensitive = true
}

variable "bedrock_enabled" {
  description = "Give the worker Bedrock access. Off until the account has model access."
  type        = bool
  default     = false
}

variable "bedrock_model" {
  type    = string
  default = "anthropic.claude-haiku-5-5"
}
