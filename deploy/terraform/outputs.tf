output "ip_address" {
  description = "Public IPv4 of the node. The deploy scripts read this."
  # linode_instance.ip_address is deprecated, and it picks an arbitrary address
  # when there is more than one. ipv4 is a set. private_ip is false, so there is
  # exactly one address in it, and one() fails loudly if that ever stops being
  # true instead of handing back the wrong address.
  value = one(linode_instance.node.ipv4)
}

output "label" {
  value = linode_instance.node.label
}

output "region" {
  value = linode_instance.node.region
}

output "instance_type" {
  value = linode_instance.node.type
}

output "deploy_user" {
  value = var.deploy_user
}

output "inbound_rules" {
  description = "What the firewall accepts. One rule in tunnel mode."
  value       = local.inbound_rules
}
