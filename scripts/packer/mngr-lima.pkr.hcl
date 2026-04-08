packer {
  required_plugins {
    qemu = {
      version = ">= 1.1.0"
      source  = "github.com/hashicorp/qemu"
    }
  }
}

variable "ubuntu_version" {
  type    = string
  default = "24.04"
}

variable "arch" {
  type    = string
  default = "amd64"
}

variable "qemu_binary" {
  type    = string
  default = ""
}

variable "accelerator" {
  type    = string
  default = "kvm"
}

variable "iso_url" {
  type    = string
  default = ""
}

variable "iso_checksum" {
  type    = string
  default = ""
}

locals {
  output_name = "mngr-lima-${var.arch == "arm64" ? "aarch64" : "x86_64"}"

  default_iso_url_amd64 = "https://cloud-images.ubuntu.com/releases/${var.ubuntu_version}/release/ubuntu-${var.ubuntu_version}-server-cloudimg-amd64.img"
  default_iso_url_arm64 = "https://cloud-images.ubuntu.com/releases/${var.ubuntu_version}/release/ubuntu-${var.ubuntu_version}-server-cloudimg-arm64.img"

  resolved_iso_url = var.iso_url != "" ? var.iso_url : (
    var.arch == "arm64" ? local.default_iso_url_arm64 : local.default_iso_url_amd64
  )

  resolved_qemu_binary = var.qemu_binary != "" ? var.qemu_binary : (
    var.arch == "arm64" ? "qemu-system-aarch64" : "qemu-system-x86_64"
  )
}

source "qemu" "mngr-lima" {
  iso_url      = local.resolved_iso_url
  iso_checksum = var.iso_checksum
  disk_image   = true

  output_directory = "output-${local.output_name}"
  vm_name          = "${local.output_name}.qcow2"

  format       = "qcow2"
  disk_size    = "10G"
  accelerator  = var.accelerator
  qemu_binary  = local.resolved_qemu_binary

  ssh_username = "ubuntu"
  ssh_timeout  = "10m"

  shutdown_command = "sudo shutdown -P now"

  headless = true
}

build {
  sources = ["source.qemu.mngr-lima"]

  provisioner "shell" {
    script = "${path.root}/provision.sh"
  }
}
