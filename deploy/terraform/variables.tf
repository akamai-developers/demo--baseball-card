# Every variable has a default but the two that cannot have one: the SSH public
# key and the addresses allowed to reach port 22. deploy.sh writes both, with
# the values from .env, into deploy.auto.tfvars.

variable "label" {
  description = "Instance label. The firewall is labelled <label>-fw."
  type        = string
  default     = "baseball-card"

  validation {
    # Linode labels: 3 to 64 characters, letters, digits, dot, dash, underscore.
    # 61 here, so <label>-fw fits too.
    condition     = can(regex("^[a-zA-Z0-9][a-zA-Z0-9._-]{2,60}$", var.label))
    error_message = "label must be 3 to 61 characters of letters, digits, dot, dash or underscore."
  }
}

variable "region" {
  description = "Akamai Cloud region. make gpu-check lists the ones that can sell the plan today."
  type        = string
  default     = "us-ord"

  validation {
    # The shape of a region id only. Which regions sell the plan changes by the
    # hour, so that is asked of the API by gpu_check.py, not written down here.
    condition     = can(regex("^[a-z]{2}-[a-z]+(-[0-9]+)?$", var.region))
    error_message = "region must be a region id such as us-ord or de-fra-2. make gpu-check lists them."
  }
}

variable "gpu_count" {
  description = "1 or 2. Picks the plan, and docker-compose.yml's GPU layout through the node's .env."
  type        = number
  default     = 1

  validation {
    condition     = contains([1, 2], var.gpu_count)
    error_message = "gpu_count must be 1 or 2. The compose file has a layout for one card and one for two."
  }
}

variable "instance_type" {
  description = "Empty for the plan gpu_count picks. Otherwise another RTX 4000 Ada plan with gpu_count cards, from LINODE_TYPE in .env."
  type        = string
  default     = ""

  validation {
    condition     = var.instance_type == "" || can(regex("^g2-gpu-rtx4000a[0-9]-[a-z]+$", var.instance_type))
    error_message = "instance_type must be empty or an RTX 4000 Ada plan, such as g2-gpu-rtx4000a1-s. make gpu-check lists them."
  }
}

variable "image" {
  description = "Base image. Ubuntu 24.04 is what the cloud-init file is written for."
  type        = string
  default     = "linode/ubuntu24.04"
}

variable "ssh_public_key" {
  description = "The public half of the deploy keypair. deploy.sh makes secrets/ssh/id_ed25519 and passes the .pub here."
  type        = string

  validation {
    condition     = can(regex("^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256) ", trimspace(var.ssh_public_key)))
    error_message = "ssh_public_key must be one line of OpenSSH public key text, not a path and not a private key."
  }
}

variable "admin_cidrs" {
  description = "The only addresses allowed to reach port 22, as IPv4 CIDR blocks. deploy.sh writes ADMIN_CIDR from .env here, split on its commas, or your public IP as a /32."
  type        = list(string)

  validation {
    condition = length(var.admin_cidrs) > 0 && alltrue([
      for block in var.admin_cidrs :
      can(regex("^[0-9]{1,3}(\\.[0-9]{1,3}){3}/[0-9]{1,2}$", block)) && can(cidrhost(block, 0))
    ])
    error_message = "admin_cidrs must be one or more IPv4 CIDR blocks, for example [\"203.0.113.4/32\"]."
  }
}

variable "domain" {
  description = "Empty is tunnel mode: only SSH is open, and you connect with make tunnel. A hostname opens 80 and 443 to the world, so Caddy can get a Let's Encrypt certificate for it."
  type        = string
  default     = ""

  validation {
    condition     = var.domain == "" || can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,63}$", var.domain))
    error_message = "domain must be empty or a lower-case hostname, for example cards.example.com."
  }
}

variable "deploy_user" {
  description = "The non-root user that owns the repo and runs Docker on the node."
  type        = string
  default     = "deploy"
}

variable "nvidia_driver_branch" {
  description = "Driver branch installed from NVIDIA's own apt repo as cuda-drivers-<branch>. 580 was the newest branch in the ubuntu2404 repo on 2026-09-21. RTX 4000 Ada needs 525 or newer."
  type        = string
  default     = "580"
}
