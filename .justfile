#!/usr/bin/env -S just --justfile

set minimum-version := '1.55.0'

set default-list
set default-script
set lazy
set quiet
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']
set dotenv-filename := 'kubernetes/apps/talos-admin/.env'

export KUBECONFIG := justfile_directory() / 'kubeconfig'
export SOPS_AGE_KEY_FILE := justfile_directory() / 'age.key'
export TALOSCONFIG := justfile_directory() / 'talos/talosconfig'

[group('SOPS')]
mod sops 'scripts/sops'

[group('Talos')]
mod talos 'talos'
