#!/usr/bin/env bash
# Runs once, from cloud-init, on the GPU node. Installs the NVIDIA driver,
# Docker, and the NVIDIA container toolkit, then holds the driver packages so an
# unattended upgrade cannot replace the kernel module under a running container.
#
# Nothing in this file is templated. cloud-init writes it base64 encoded and
# calls it with two arguments, so shellcheck lints the real thing.
#
# Usage: bootstrap.sh <nvidia-driver-branch> <deploy-user>
set -euo pipefail

DRIVER_BRANCH="${1:?usage: bootstrap.sh <nvidia-driver-branch> <deploy-user>}"
DEPLOY_USER="${2:?usage: bootstrap.sh <nvidia-driver-branch> <deploy-user>}"
STATE_DIR=/var/lib/baseball-card
LOG=/var/log/baseball-card-bootstrap.log

exec > >(tee -a "$LOG") 2>&1
echo "=== bootstrap started $(date -Is): driver branch $DRIVER_BRANCH, user $DEPLOY_USER"

export DEBIAN_FRONTEND=noninteractive
mkdir -p "$STATE_DIR"

# apt runs while cloud-init's own package module may still hold the lock.
apt_get() { apt-get -o DPkg::Lock::Timeout=600 "$@"; }

# noble for Ubuntu 24.04. Read rather than hardcoded so a different image fails
# on a missing suite instead of quietly installing the wrong packages.
# shellcheck source=/dev/null
. /etc/os-release
: "${VERSION_CODENAME:?/etc/os-release has no VERSION_CODENAME}"

# ---- prerequisites ----------------------------------------------------------
apt_get update
apt_get install -y --no-install-recommends ca-certificates curl gnupg rsync jq

install -m 0755 -d /etc/apt/keyrings

# ---- Docker -----------------------------------------------------------------
# From Docker's own repo, not Ubuntu's. Ubuntu ships docker.io, which does not
# carry the compose v2 plugin this stack is built on.
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
cat >/etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: ${VERSION_CODENAME}
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF

# ---- NVIDIA driver ----------------------------------------------------------
# NVIDIA's CUDA repo rather than Ubuntu's restricted pocket, because the branch
# is pinnable there and the newest branch is always new enough for Ada.
curl -fsSL -o /tmp/cuda-keyring.deb \
	https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
dpkg -i /tmp/cuda-keyring.deb
rm -f /tmp/cuda-keyring.deb

# ---- NVIDIA container toolkit ----------------------------------------------
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
	| gpg --dearmor -o /etc/apt/keyrings/nvidia-container-toolkit.gpg
cat >/etc/apt/sources.list.d/nvidia-container-toolkit.list <<'EOF'
deb [signed-by=/etc/apt/keyrings/nvidia-container-toolkit.gpg] https://nvidia.github.io/libnvidia-container/stable/deb/amd64 /
EOF

apt_get update

# ---- what dkms needs to build the kernel module -----------------------------
# cuda-drivers-* builds the nvidia module with dkms, and dkms only *recommends*
# a C compiler, so --no-install-recommends leaves the box without one. The build
# then stops at "Failed CC sanity check. Bailing out!", the nvidia packages are
# left unconfigured, and the first sign of it is nvidia-smi listing no GPUs after
# the reboot. Measured on a fresh linode/ubuntu24.04 on 2026-09-21.
#
# Its own apt transaction, ahead of the driver. In one transaction dpkg is free
# to configure nvidia-dkms before gcc, since nothing declares that order.
apt_get install -y --no-install-recommends \
	build-essential "linux-headers-$(uname -r)"

apt_get install -y --no-install-recommends \
	"cuda-drivers-${DRIVER_BRANCH}" \
	docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
	nvidia-container-toolkit

# The module is built now, or it is not and nothing below is worth doing. dkms
# says "installed" only for a module that compiled and landed in /lib/modules.
# A branch that ships precompiled modules instead has no nvidia entry at all,
# which is why this only judges a dkms build that is actually there.
if dkms status 2>/dev/null | grep -q '^nvidia/'; then
	dkms status | grep -q '^nvidia/.*: installed' || {
		echo "dkms did not build the nvidia module:" >&2
		dkms status >&2
		echo "the compiler errors are in /var/lib/dkms/nvidia/*/build/make.log" >&2
		exit 1
	}
	echo "dkms built the nvidia module: $(dkms status | grep '^nvidia/')"
fi

# ---- wire the toolkit into Docker ------------------------------------------
# Writes the nvidia runtime into /etc/docker/daemon.json. Without it, the
# deploy.resources.reservations.devices blocks in docker-compose.yml have no
# driver to call and every GPU service fails to start.
nvidia-ctk runtime configure --runtime=docker
systemctl enable --now docker
systemctl restart docker

# ---- hold the driver --------------------------------------------------------
# A driver upgrade swaps the kernel module out from under running containers and
# every CUDA call starts failing with a version mismatch until the box reboots.
# Hold whatever the install actually put on the box: the package set differs
# between the dkms and the precompiled-module flavours of a branch, so a
# hardcoded list would fail on one of them.
mapfile -t nvidia_packages < <(
	dpkg-query -W -f '${Package} ${Status}\n' 'nvidia-*' 'libnvidia-*' 'cuda-*' 2>/dev/null \
		| awk '$4 == "installed" { print $1 }'
)
if [ "${#nvidia_packages[@]}" -eq 0 ]; then
	echo "no nvidia packages are installed. the driver install did not work." >&2
	exit 1
fi
echo "holding ${#nvidia_packages[@]} driver packages"
apt-mark hold "${nvidia_packages[@]}"

cat >/etc/apt/apt.conf.d/51-baseball-card-hold-nvidia <<'EOF'
// The NVIDIA driver is held with apt-mark. See deploy/cloud-init/bootstrap.sh.
Unattended-Upgrade::Package-Blacklist {
	"^nvidia-";
	"^libnvidia-";
	"^cuda-";
	"^linux-modules-nvidia-";
};
EOF

# ---- where the repo lands ---------------------------------------------------
# deploy.sh rsyncs into here as the deploy user.
install -d -m 0755 /opt/baseball-card
chown "$DEPLOY_USER:$(id -gn "$DEPLOY_USER")" /opt/baseball-card

echo "=== bootstrap finished $(date -Is)"
# The reboot is next, from cloud-init's power_state. The kernel module has to
# displace nouveau, which is already bound to the cards from the first boot.
# bootcmd turns this marker into the "ready" one on the boot after this, so
# deploy.sh can tell "installed" apart from "installed and rebooted".
date -Is >"$STATE_DIR/installed"
