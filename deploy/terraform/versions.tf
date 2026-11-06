terraform {
  # 1.7 for the tests in tests/: they replace the Linode provider with a mock,
  # and mock_provider arrived in 1.7.
  required_version = ">= 1.7"

  required_providers {
    linode = {
      source = "linode/linode"
      # .terraform.lock.hcl pins the exact build, 4.5.0.
      version = "~> 4.5"
    }
  }
}

# The token is read from LINODE_TOKEN in the environment. It is never written
# into a file here, and there is no *.tfvars for it. deploy/scripts/lib.sh
# checks that the variable is set before it runs terraform at all.
provider "linode" {}
