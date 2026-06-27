// Packer template for the pre-baked Lima VM image (issue #2306).
//
// Boots the same Debian 12 "bookworm" genericcloud base the Lima provider uses
// (so cloud-init + the Lima guest agent keep working), bakes the full
// forever-claude-template toolchain into it by running the exact FCT build
// scripts, then leaves a generic-per-release image. The user's repo is injected
// at create time -- only the release toolchain + vendored mngr are baked, so the
// image stays generic per `minds-v<version>`.
//
// Build one arch per native host: amd64 on a KVM-enabled Linux host, arm64 on an
// Apple-Silicon (HVF) host. QEMU TCG cross-builds of this heavy image are far too
// slow to be practical.

packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "arch" {
  type    = string
  default = "amd64"
}

variable "accelerator" {
  type    = string
  default = "kvm" // "kvm" on Linux/amd64, "hvf" on macOS/arm64, "tcg" only as a last resort
}

variable "qemu_binary" {
  type    = string
  default = ""
}

variable "iso_url" {
  type    = string
  default = ""
}

variable "iso_checksum" {
  type = string
  // Debian publishes SHA512SUMS next to each image; pass the matching checksum
  // (e.g. "file:https://.../SHA512SUMS") for a verified base. "none" is accepted
  // for local smoke builds only.
  default = "none"
}

variable "fct_repo_url" {
  type    = string
  default = "https://github.com/imbue-ai/forever-claude-template.git"
}

variable "fct_ref" {
  type = string // the minds-v<version> tag to bake, e.g. "minds-v0.3.4"
}

variable "disk_size" {
  type    = string
  default = "24G" // the FCT toolchain + Playwright/Chromium need well over the base image
}

variable "provision_script" {
  type    = string
  default = "provision.sh" // override (relative to this dir) for smoke builds
}

locals {
  output_name = "mngr-lima-${var.arch == "arm64" ? "aarch64" : "x86_64"}"

  // Debian 12 genericcloud images, matching libs/mngr_lima/.../constants.py.
  default_iso_url_amd64 = "https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-amd64-20260601-2496.qcow2"
  default_iso_url_arm64 = "https://cloud.debian.org/images/cloud/bookworm/20260601-2496/debian-12-genericcloud-arm64-20260601-2496.qcow2"

  resolved_iso_url = var.iso_url != "" ? var.iso_url : (
    var.arch == "arm64" ? local.default_iso_url_arm64 : local.default_iso_url_amd64
  )
  resolved_qemu_binary = var.qemu_binary != "" ? var.qemu_binary : (
    var.arch == "arm64" ? "qemu-system-aarch64" : "qemu-system-x86_64"
  )
  machine_type = var.arch == "arm64" ? "virt" : "pc"
}

source "qemu" "mngr-lima" {
  iso_url      = local.resolved_iso_url
  iso_checksum = var.iso_checksum
  disk_image   = true

  output_directory = "output-${local.output_name}"
  vm_name          = "${local.output_name}.qcow2"

  format       = "qcow2"
  disk_size    = var.disk_size
  accelerator  = var.accelerator
  qemu_binary  = local.resolved_qemu_binary
  machine_type = local.machine_type

  // Debian cloud images ship the "debian" user; cloud-init below sets a
  // throwaway build-only password + passwordless sudo so Packer's SSH comes up
  // unattended. The password never leaves the transient build VM (the baked
  // image regenerates host keys + has password auth off in the provider path).
  ssh_username = "debian"
  ssh_password = "packer-build-mngr-lima"
  ssh_timeout  = "10m"

  cd_label = "cidata"
  cd_content = {
    "meta-data" = ""
    "user-data" = <<-EOF
      #cloud-config
      ssh_pwauth: true
      chpasswd:
        expire: false
      users:
        - name: debian
          sudo: ALL=(ALL) NOPASSWD:ALL
          lock_passwd: false
          plain_text_passwd: packer-build-mngr-lima
    EOF
  }

  shutdown_command = "sudo shutdown -P now"
  headless         = true
}

build {
  sources = ["source.qemu.mngr-lima"]

  provisioner "shell" {
    environment_vars = [
      "FCT_REPO_URL=${var.fct_repo_url}",
      "FCT_REF=${var.fct_ref}",
    ]
    execute_command = "chmod +x {{ .Path }}; sudo -E bash '{{ .Path }}'"
    script          = "${path.root}/${var.provision_script}"
  }
}
