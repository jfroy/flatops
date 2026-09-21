set default-list
set default-script
set quiet
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

# Encrypt unencrypted SOPS files in bootstrap/ and talos/, including topf.yaml's data
encrypt:
    cd {{ quote(justfile_directory()) }}
    for tool in sops jq; do
        command -v "$tool" >/dev/null || { echo "Missing $tool" >&2; exit 1; }
    done
    test -f .sops.yaml || { echo 'Missing SOPS config file' >&2; exit 1; }
    test -f "$SOPS_AGE_KEY_FILE" || { echo 'Missing SOPS age key file' >&2; exit 1; }
    files=(talos/topf.yaml)
    for dir in bootstrap talos; do
        if [[ -d "$dir" ]]; then
            while IFS= read -r -d '' file; do
                files+=("$file")
            done < <(find "$dir" -type f -name '*.sops.*' -print0)
        fi
    done
    for file in "${files[@]}"; do
        status="$(sops filestatus "$file")"
        if jq --exit-status '.encrypted == false' <<< "$status" >/dev/null; then
            sops --encrypt --in-place "$file"
        fi
    done
