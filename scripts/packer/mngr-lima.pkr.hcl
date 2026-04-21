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

variable "claude_code_version" {
  type        = string
  default     = ""
  description = "Claude Code version to pin, passed to claude.ai/install.sh. Empty means latest."
}

variable "qemu_binary" {
  type    = string
  default = ""
}

variable "accelerator" {
  type        = string
  default     = "tcg"
  description = "QEMU accelerator. Override per host: 'hvf' on macOS, 'kvm' on Linux. 'tcg' is emulated (slow) but universal."
}

variable "iso_url" {
  type    = string
  default = ""
}

variable "iso_checksum" {
  type    = string
  default = ""
}

variable "firmware" {
  type        = string
  default     = ""
  description = "Path to UEFI firmware .fd file. Empty auto-detects based on arch + Homebrew path."
}

variable "machine_type" {
  type        = string
  default     = ""
  description = "QEMU machine type. Empty auto-detects: 'virt' on arm64, 'pc' on amd64."
}

variable "cpu_model" {
  type        = string
  default     = "host"
  description = "QEMU CPU model. 'host' for native accel, 'max' for emulation."
}

locals {
  output_name = "mngr-lima-${var.arch == "arm64" ? "aarch64" : "x86_64"}"

  default_iso_url_amd64 = "https://cloud-images.ubuntu.com/releases/${var.ubuntu_version}/release/ubuntu-${var.ubuntu_version}-server-cloudimg-amd64.img"
  default_iso_url_arm64 = "https://cloud-images.ubuntu.com/releases/${var.ubuntu_version}/release/ubuntu-${var.ubuntu_version}-server-cloudimg-arm64.img"

  # Default SHA256 of the stock Ubuntu cloud image for each arch, pulled from
  # https://cloud-images.ubuntu.com/releases/${ubuntu_version}/release/SHA256SUMS.
  # Canonical rotates these when they rebuild the Ubuntu 24.04 cloud image, so
  # when the build fails checksum verification the fix is to pull the current
  # hash from that URL and update here. We want the build to FAIL when Canonical
  # refreshes rather than silently consuming unexpected bytes.
  default_iso_checksum_amd64 = "sha256:5c3ddb00f60bc455dac0862fabe9d8bacec46c33ac1751143c5c3683404b110d"
  default_iso_checksum_arm64 = "sha256:1ea801e659d2f5035ac294e0faab0aac9b6ba66753df933ba5c7beab0c689bd0"

  # Homebrew's QEMU ships UEFI firmware at /opt/homebrew/share/qemu/ on macOS;
  # on Linux/apt distributions it's typically /usr/share/OVMF/ or similar. Ubuntu
  # cloud images are UEFI-only on both arches in 22.04+, so firmware is required.
  default_firmware_amd64 = "/opt/homebrew/share/qemu/edk2-x86_64-code.fd"
  default_firmware_arm64 = "/opt/homebrew/share/qemu/edk2-aarch64-code.fd"

  resolved_iso_url = var.iso_url != "" ? var.iso_url : (
    var.arch == "arm64" ? local.default_iso_url_arm64 : local.default_iso_url_amd64
  )

  resolved_iso_checksum = var.iso_checksum != "" ? var.iso_checksum : (
    var.arch == "arm64" ? local.default_iso_checksum_arm64 : local.default_iso_checksum_amd64
  )

  resolved_qemu_binary = var.qemu_binary != "" ? var.qemu_binary : (
    var.arch == "arm64" ? "qemu-system-aarch64" : "qemu-system-x86_64"
  )

  resolved_firmware = var.firmware != "" ? var.firmware : (
    var.arch == "arm64" ? local.default_firmware_arm64 : local.default_firmware_amd64
  )

  resolved_machine_type = var.machine_type != "" ? var.machine_type : (
    var.arch == "arm64" ? "virt" : "pc"
  )
}

source "qemu" "mngr-lima" {
  iso_url      = local.resolved_iso_url
  iso_checksum = local.resolved_iso_checksum
  disk_image   = true

  output_directory = "output-${local.output_name}"
  vm_name          = "${local.output_name}.qcow2"

  format       = "qcow2"
  disk_size    = "20G"
  accelerator  = var.accelerator
  qemu_binary  = local.resolved_qemu_binary
  machine_type = local.resolved_machine_type
  firmware     = local.resolved_firmware
  cpu_model    = var.cpu_model
  memory       = 4096
  cpus         = 2

  ssh_username = "ubuntu"
  ssh_timeout  = "15m"

  shutdown_command = "sudo shutdown -P now"

  headless = true
}

build {
  sources = ["source.qemu.mngr-lima"]

  provisioner "shell" {
    script = "${path.root}/provision.sh"
    environment_vars = [
      "CLAUDE_CODE_VERSION=${var.claude_code_version}",
    ]
  }
}
