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

variable "seed_iso" {
  type        = string
  default     = ""
  description = "Path to pre-built cloud-init seed ISO. Built by the build script."
}

locals {
  output_name = "mngr-lima-${var.arch == "arm64" ? "aarch64" : "x86_64"}"

  # Alpine 3.23 cloud images (matches Lima's own Alpine template)
  default_iso_url_amd64 = "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/nocloud_alpine-3.23.3-x86_64-uefi-cloudinit-r0.qcow2"
  default_iso_url_arm64 = "https://dl-cdn.alpinelinux.org/alpine/v3.23/releases/cloud/nocloud_alpine-3.23.3-aarch64-uefi-cloudinit-r0.qcow2"

  resolved_iso_url = var.iso_url != "" ? var.iso_url : (
    var.arch == "arm64" ? local.default_iso_url_arm64 : local.default_iso_url_amd64
  )

  resolved_qemu_binary = var.qemu_binary != "" ? var.qemu_binary : (
    var.arch == "arm64" ? "qemu-system-aarch64" : "qemu-system-x86_64"
  )
}

source "qemu" "mngr-lima" {
  iso_url      = local.resolved_iso_url
  iso_checksum = var.iso_checksum != "" ? var.iso_checksum : "none"
  disk_image   = true

  output_directory = "output-${local.output_name}"
  vm_name          = "${local.output_name}.qcow2"

  format       = "qcow2"
  disk_size    = "10G"
  accelerator  = var.accelerator
  qemu_binary  = local.resolved_qemu_binary

  # Serve cloud-init data via packer's built-in HTTP server.
  # The QEMU SMBIOS setting tells cloud-init where to find it.
  http_directory = "${path.root}/http"

  # UEFI firmware required by Alpine cloud images.
  machine_type = "q35"
  qemuargs = [
    ["-bios", "/usr/share/OVMF/OVMF_CODE.fd"],
    ["-smbios", "type=1,serial=ds=nocloud-net;s=http://{{ .HTTPIP }}:{{ .HTTPPort }}/"],
  ]

  ssh_username = "root"
  ssh_password = "packer"
  ssh_timeout  = "10m"

  shutdown_command = "sudo poweroff"

  headless = true
}

build {
  sources = ["source.qemu.mngr-lima"]

  provisioner "shell" {
    script = "${path.root}/provision.sh"
  }

  # Clean up cloud-init artifacts so the image is ready for Lima's
  # own cloud-init to run on first boot.
  provisioner "shell" {
    inline = [
      "cloud-init clean --logs 2>/dev/null || true",
      "passwd -l root",
    ]
  }
}
