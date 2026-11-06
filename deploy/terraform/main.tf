# One GPU instance and one firewall in front of it. Nothing else.
#
# State is local and gitignored. There is no remote backend, because a demo that
# needs a bucket before it can deploy is a demo nobody runs.

locals {
  # Fixed, not a variable. deploy/scripts/destroy.sh asks the API whether
  # anything with this tag is left, and the scripts never touch anything
  # without it. A variable here would let a typo create resources that check
  # cannot see.
  tag = "baseball-card"

  # The plan each GPU count gets. One card with 32 GB of RAM, because the
  # one-card layout keeps FLUX's 16 GB of weights in RAM. Two cards are the
  # plan the two-card layout was measured on. deploy/scripts/gpu_check.py holds
  # the same two, and tests/security/test_terraform.py holds them equal.
  plans = {
    1 = "g2-gpu-rtx4000a1-m"
    2 = "g2-gpu-rtx4000a2-s"
  }
  instance_type = var.instance_type != "" ? var.instance_type : local.plans[var.gpu_count]

  domain_mode = var.domain != ""

  # Tunnel mode is one inbound rule. Domain mode adds two, because Let's Encrypt
  # has to reach port 80 for the HTTP challenge and browsers have to reach 443.
  ssh_rule = [{
    label    = "ssh-admin"
    protocol = "TCP"
    ports    = "22"
    ipv4     = [var.admin_cidr]
  }]

  web_rules = local.domain_mode ? [
    {
      label    = "http-acme-and-redirect"
      protocol = "TCP"
      ports    = "80"
      ipv4     = ["0.0.0.0/0"]
    },
    {
      label    = "https"
      protocol = "TCP"
      ports    = "443"
      ipv4     = ["0.0.0.0/0"]
    },
  ] : []

  inbound_rules = concat(local.ssh_rule, local.web_rules)

  user_data = templatefile("${path.module}/../cloud-init/user-data.yaml.tftpl", {
    hostname = var.label
    # The script is a real file so shellcheck can lint it and a person can read
    # it without a template in the way. It arrives base64 encoded, so nothing in
    # it needs YAML or Terraform escaping.
    bootstrap_script     = file("${path.module}/../cloud-init/bootstrap.sh")
    ssh_public_key       = trimspace(var.ssh_public_key)
    deploy_user          = var.deploy_user
    nvidia_driver_branch = var.nvidia_driver_branch
  })
}

resource "linode_instance" "node" {
  label  = var.label
  region = var.region
  type   = local.instance_type
  image  = var.image
  tags   = [local.tag]

  # The Linode API wants root_pass or authorized_keys when an image is given.
  # This is the same deploy key, and cloud-init turns root SSH off a minute
  # later. A generated root password would be a second secret to look after for
  # no gain.
  authorized_keys = [trimspace(var.ssh_public_key)]

  # No private networking. Nothing else in the account has any business reaching
  # this node, and a 192.168.128.0/17 address is one more way in.
  private_ip = false

  # Legacy config interfaces, so linode_firewall.linodes attaches the rules to
  # the instance. With interface_generation = "linode" a firewall binds to an
  # individual interface instead, and the account-wide default for new Linodes
  # can change under you. Pinning it keeps the firewall attached either way.
  interface_generation = "legacy_config"

  metadata {
    user_data = base64encode(local.user_data)
  }

  lifecycle {
    # docker-compose.yml places the models by the layout for gpu_count, so a
    # plan with another number of cards would leave a model with no card, or a
    # card with no model.
    precondition {
      condition     = startswith(local.instance_type, "g2-gpu-rtx4000a${var.gpu_count}-")
      error_message = "The plan ${local.instance_type} does not have ${var.gpu_count} GPUs, and GPU_COUNT is ${var.gpu_count}. Set LINODE_TYPE to a plan with that many cards, or empty for the default."
    }
  }
}

resource "linode_firewall" "node" {
  label = "${var.label}-fw"
  tags  = [local.tag]

  # Drop everything inbound that no rule below accepts. Outbound is open,
  # because the node has to reach Hugging Face for 27 GB of weights, Docker Hub
  # for images, and apt for the driver.
  inbound_policy  = "DROP"
  outbound_policy = "ACCEPT"

  dynamic "inbound" {
    for_each = local.inbound_rules

    content {
      label    = inbound.value.label
      action   = "ACCEPT"
      protocol = inbound.value.protocol
      ports    = inbound.value.ports
      ipv4     = inbound.value.ipv4
    }
  }

  linodes = [linode_instance.node.id]
}
