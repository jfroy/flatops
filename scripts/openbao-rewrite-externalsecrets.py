#!/usr/bin/env python3
"""Rewrite ExternalSecret manifests from the `onepassword` ClusterSecretStore to `openbao`.

Two mechanical transforms, applied with plain text edits so formatting, comments
and document order are preserved exactly:

  1. `secretStoreRef: {kind: ClusterSecretStore, name: onepassword}` -> `name: openbao`
  2. `remoteRef.key: <item>/<field>` (1Password SDK "item/field" addressing)
     -> `remoteRef.key: <item>` + `remoteRef.property: <field>` (Vault KV v2 addressing)

  3. `--base64 <item>` (repeatable): add `decodingStrategy: Base64` to every reference to
     <item>, for items openbao-migrate.py stored base64-encoded (binary Document files).

Usage: scripts/openbao-rewrite-externalsecrets.py [--check] [--base64 ITEM]... [paths...]
  Default path is ./kubernetes. --check reports what would change and exits 1 if anything would.
"""
import pathlib
import re
import sys

STORE_RE = re.compile(r"^(?P<indent>\s+)name: onepassword\s*$")
KEY_RE = re.compile(r'^(?P<indent>\s+)key: (?P<q>"?)(?P<item>[^"/\s]+)/(?P<field>[^"\s]+)(?P=q)\s*$')


BARE_KEY_RE = re.compile(r'^(?P<indent>\s+)key: "?(?P<item>[^"/\s]+)"?\s*$')


def rewrite(text: str, base64_items: set[str] = frozenset()):
    out, stores, keys = [], 0, 0
    lines = text.splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = BARE_KEY_RE.match(line)
        if m and m["item"] in base64_items and i > 0 and lines[i - 1].strip() in ("remoteRef:", "extract:", "- extract:"):
            out.append(line)
            if not (i + 1 < len(lines) and lines[i + 1].strip().startswith("decodingStrategy:")):
                out.append(f"{m['indent']}decodingStrategy: Base64\n")
            continue
        m = STORE_RE.match(line)
        if m and i > 1 and lines[i - 1].strip() == "kind: ClusterSecretStore" and lines[i - 2].strip() == "secretStoreRef:":
            out.append(f"{m['indent']}name: openbao\n")
            stores += 1
            continue
        m = KEY_RE.match(line)
        if m and i > 0 and lines[i - 1].strip() == "remoteRef:":
            q = m["q"]
            out.append(f"{m['indent']}key: {q}{m['item']}{q}\n")
            out.append(f"{m['indent']}property: {q}{m['field']}{q}\n")
            keys += 1
            continue
        out.append(line)
    return "".join(out), stores, keys


def main(argv):
    check = "--check" in argv
    base64_items = {argv[i + 1] for i, a in enumerate(argv) if a == "--base64"}
    paths = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--base64")] or ["kubernetes"]
    files = sorted({p for base in paths for p in pathlib.Path(base).rglob("*.yaml")})
    total_stores = total_keys = changed = 0
    for f in files:
        text = f.read_text()
        if "onepassword" not in text and "/" not in text:
            continue
        new, stores, keys = rewrite(text, base64_items)
        if new != text:
            changed += 1
            total_stores += stores
            total_keys += keys
            if check:
                print(f"{f}: {stores} store refs, {keys} item/field keys")
            else:
                f.write_text(new)
    print(f"{'would change' if check else 'changed'} {changed} files: {total_stores} store refs -> openbao, {total_keys} item/field keys -> key+property")
    return 1 if (check and changed) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
