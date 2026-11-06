# The instance and its firewall, planned against a stand-in provider.
#
# mock_provider replaces linode whole: no token is read, no API is called,
# nothing is created, and the real terraform.tfstate is neither read nor
# written. Every run below is a plan. tests/security/test_terraform.py runs this
# file with LINODE_TOKEN taken out of the environment, so a provider someone
# forgets to mock here has nothing to log in with.
#
#   terraform -chdir=deploy/terraform init
#   terraform -chdir=deploy/terraform test

mock_provider "linode" {}

variables {
  ssh_public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExampleExampleExampleExampleExampleEx test"
  admin_cidr     = "203.0.113.4/32"
}

run "tunnel_mode_opens_port_22_to_one_address_and_nothing_else" {
  command = plan

  assert {
    condition     = length(linode_firewall.node.inbound) == 1
    error_message = "Tunnel mode must have exactly one inbound rule."
  }

  assert {
    condition     = linode_firewall.node.inbound[0].ports == "22" && linode_firewall.node.inbound[0].protocol == "TCP"
    error_message = "The one rule is not TCP 22."
  }

  assert {
    condition     = linode_firewall.node.inbound[0].ipv4 == tolist(["203.0.113.4/32"])
    error_message = "Port 22 is open to something other than admin_cidr."
  }

  assert {
    condition     = linode_firewall.node.inbound_policy == "DROP"
    error_message = "The firewall does not drop what no rule accepts."
  }
}

run "domain_mode_adds_80_and_443_and_keeps_22_to_one_address" {
  command = plan

  variables {
    domain = "cards.example.com"
  }

  assert {
    condition     = [for rule in linode_firewall.node.inbound : rule.ports] == ["22", "80", "443"]
    error_message = "Domain mode must open 22, 80 and 443, in that order, and nothing else."
  }

  assert {
    condition     = linode_firewall.node.inbound[0].ipv4 == tolist(["203.0.113.4/32"])
    error_message = "Domain mode opened port 22 beyond admin_cidr."
  }

  assert {
    condition     = alltrue([for rule in slice(linode_firewall.node.inbound, 1, 3) : rule.ipv4 == tolist(["0.0.0.0/0"]) && rule.protocol == "TCP"])
    error_message = "80 and 443 must be TCP from anywhere, so Let's Encrypt and browsers can reach them."
  }
}

run "one_gpu_is_the_one_card_plan" {
  command = plan

  assert {
    condition     = linode_instance.node.type == "g2-gpu-rtx4000a1-m"
    error_message = "gpu_count = 1 must deploy g2-gpu-rtx4000a1-m."
  }
}

run "two_gpus_is_the_two_card_plan" {
  command = plan

  variables {
    gpu_count = 2
  }

  assert {
    condition     = linode_instance.node.type == "g2-gpu-rtx4000a2-s"
    error_message = "gpu_count = 2 must deploy g2-gpu-rtx4000a2-s."
  }
}

run "another_plan_with_the_same_number_of_cards_can_be_named" {
  command = plan

  variables {
    gpu_count     = 1
    instance_type = "g2-gpu-rtx4000a1-s"
  }

  assert {
    condition     = linode_instance.node.type == "g2-gpu-rtx4000a1-s"
    error_message = "LINODE_TYPE did not reach the instance."
  }
}

run "a_plan_with_another_number_of_cards_is_refused" {
  command = plan

  variables {
    gpu_count     = 1
    instance_type = "g2-gpu-rtx4000a2-s"
  }

  expect_failures = [linode_instance.node]
}

run "three_gpus_is_refused" {
  command = plan

  variables {
    gpu_count = 3
  }

  expect_failures = [var.gpu_count]
}

run "every_resource_carries_the_tag" {
  command = plan

  assert {
    condition     = linode_instance.node.tags == toset(["baseball-card"])
    error_message = "The instance must carry the tag baseball-card, and only it, or destroy.sh cannot find it."
  }

  assert {
    condition     = linode_firewall.node.tags == toset(["baseball-card"])
    error_message = "The firewall must carry the tag baseball-card, and only it, or destroy.sh cannot find it."
  }
}

run "the_node_has_no_private_address" {
  command = plan

  assert {
    condition     = linode_instance.node.private_ip == false
    error_message = "A private address is one more way in."
  }
}
