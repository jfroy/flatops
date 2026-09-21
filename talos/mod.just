set default-list
set default-script
set quiet
set script-interpreter := ['bash', '-euo', 'pipefail']
set shell := ['bash', '-euo', 'pipefail', '-c']

# List nodes and their current state (omit hostname for all nodes)
nodes hostname='': _check-secrets
    topf nodes {{ if hostname == '' { '' } else { '--nodes-filter ' + quote('^' + hostname + '$') } }}

# Render machine configs to talos/output, which holds plaintext secrets
[arg('online', long, value='true')]
render hostname='' online='false': _check-secrets
    topf render --output {{ quote(source_directory() / 'output') }} {{ if hostname == '' { '' } else { '--nodes-filter ' + quote('^' + hostname + '$') } }} {{ if online == 'true' { '--online' } else { '' } }}
    echo 'NOTE: talos/output holds cluster secrets in plaintext -- delete it when done.' >&2

# Diff the rendered config against live nodes without applying
diff hostname='': _check-secrets
    topf apply --dry-run {{ if hostname == '' { '' } else { '--nodes-filter ' + quote('^' + hostname + '$') } }}

# Apply machine configs (topf prompts before applying)
[arg('mode', long, pattern='auto|reboot|no-reboot|staged|try')]
apply hostname='' mode='auto': _check-secrets
    topf apply --mode={{ quote(mode) }} {{ if hostname == '' { '' } else { '--nodes-filter ' + quote('^' + hostname + '$') } }}

# Upgrade Talos to the version in topf.yaml (topf prompts before upgrading)
upgrade hostname='': _check-secrets
    topf upgrade {{ if hostname == '' { '' } else { '--nodes-filter ' + quote('^' + hostname + '$') } }}

# Print all resolved schematic IDs (topf does not support filtering these by node)
schematic-ids: _check
    topf schematic-ids

# Regenerate talos/talosconfig from the secrets bundle
talosconfig: _check-secrets
    topf talosconfig > "$TALOSCONFIG"

# Print a 12h admin kubeconfig (redirect it yourself; does not touch ./kubeconfig)
kubeconfig: _check-secrets
    topf kubeconfig

[private]
_check:
    command -v topf >/dev/null || { echo 'Missing topf' >&2; exit 1; }
    test -f topf.yaml || { echo 'Missing talos/topf.yaml' >&2; exit 1; }

[private]
_check-secrets: _check
    test -f secrets.sops.yaml || { echo 'Missing Talos secrets bundle' >&2; exit 1; }
    test -f "$SOPS_AGE_KEY_FILE" && test -f {{ quote(justfile_directory() / '.sops.yaml') }} || { echo 'Missing SOPS age key or config' >&2; exit 1; }
