#!/usr/bin/env python3
"""Copy the 1Password `kantai` vault into OpenBao KV v2 (mount kantai/) and verify.

Usage: scripts/openbao-migrate.py copy     one KV secret per 1Password item referenced by kubernetes/,
                                           fields keyed by label (files by name for Document items)
       scripts/openbao-migrate.py verify   every referenced item matches OpenBao field for field, and
                                           every remoteRef in kubernetes/ resolves in OpenBao

Requires: python3 (stdlib only) and the 1Password CLI `op` signed in to the kantai vault.
  BAO_ADDR   OpenBao address (default https://bao.etincelle.cloud)
  BAO_TOKEN  a token with create/update/read/list on kantai/data/* and kantai/metadata/*
             (e.g. one minted by a re-run of etincelle's openbao-init.py before it revokes
             root, or a token from a dedicated `kantai-migrate` policy). Revoke it afterwards.

Semantics mirror the ESO onepasswordSDK provider the manifests were written against: a
field is addressed by its *label*; `dataFrom.extract.key: item` yields every labelled field
with a non-empty value; `key: item/field` yields one. Items whose labels collide abort,
since the SDK provider would be ambiguous there too. Document items copy their attached
files keyed by file name, matching what the provider returns for them.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OP_VAULT = "kantai"
KV_MOUNT = "kantai"
KEY_RE = re.compile(r'^\s+key: "?([^"\s]+)"?\s*$')
PROP_RE = re.compile(r'^\s+property: "?([^"\s]+)"?\s*$')
KEY_PARENTS = {"remoteRef:", "extract:", "- extract:"}
STORE_RE = re.compile(r"^\s+name: (onepassword|openbao)\s*$")


class Bao:
    def __init__(self, addr: str, token: str) -> None:
        self.addr, self.token = addr.rstrip("/"), token

    def request(self, method: str, path: str, data: dict | None = None) -> dict | None:
        """Return the JSON body, None on 404, raise on any other error."""
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.addr}/v1/{path}", data=body, method=method,
                                     headers={"X-Vault-Token": self.token, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            sys.exit(f"ERROR: {method} {path}: HTTP {e.code}: {e.read().decode(errors='replace')}")
        except urllib.error.URLError as e:
            sys.exit(f"ERROR: {method} {path}: {e.reason}")
        return json.loads(raw) if raw else {}

    def kv_get(self, name: str) -> dict[str, str] | None:
        resp = self.request("GET", f"{KV_MOUNT}/data/{urllib.parse.quote(name, safe='')}")
        return None if resp is None else resp["data"]["data"]

    def kv_put(self, name: str, fields: dict[str, str]) -> None:
        self.request("POST", f"{KV_MOUNT}/data/{urllib.parse.quote(name, safe='')}", {"data": fields})


def op(*args: str) -> str:
    return subprocess.run(["op", *args, "--vault", OP_VAULT, "--format", "json"],
                          check=True, capture_output=True, text=True).stdout


def item_titles() -> list[str]:
    return sorted(i["title"] for i in json.loads(op("item", "list")))


def item_fields(title: str) -> dict[str, str]:
    """What the ESO onepasswordSDK provider yields for `dataFrom.extract.key: <title>`:
    {file name: content} for a Document item, else {label: value} for labelled, non-empty fields."""
    item = json.loads(op("item", "get", title))
    if item.get("category") == "DOCUMENT":
        return {f["name"]: file_content(title, f["name"]) for f in item.get("files", [])}
    fields = [f for f in item.get("fields", []) if f.get("label") and f.get("value")]
    labels = [f["label"] for f in fields]
    dups = sorted({l for l in labels if labels.count(l) > 1})
    if dups:
        sys.exit(f"ERROR: {title}: duplicate field labels {dups}; disambiguate in 1Password first")
    return {f["label"]: f["value"] for f in fields}


BASE64_ITEMS: set[str] = set()


def file_content(title: str, name: str) -> str:
    """Content of the single file attached to a Document item; binary content is base64-encoded
    and the item recorded in BASE64_ITEMS (its ExternalSecret then needs decodingStrategy: Base64)."""
    # `op document get` returns the raw bytes; `op read` would append a newline.
    raw = subprocess.run(["op", "document", "get", title, "--vault", OP_VAULT],
                         check=True, capture_output=True).stdout
    try:
        return raw.decode()
    except UnicodeDecodeError:
        BASE64_ITEMS.add(title)
        return base64.b64encode(raw).decode()


NAME_RE = re.compile(r"^  name: (?:&\w+ )?([\w.-]+)\s*$")
APP_RE = re.compile(r"^\s+APP: (?:\*\w+|([\w.-]+))\s*$")


def component_refs(root: Path = Path("kubernetes")) -> list[tuple[Path, str, str]]:
    """References made through components/envoy-gateway-oidc, whose ExternalSecret reads
    `${APP}-oidc/client-id` and `/client-secret`: one pair per Flux Kustomization that lists the
    component, with APP resolved from postBuild.substitute (or the Kustomization name)."""
    refs = []
    for path in sorted(root.rglob("ks.yaml")):
        for doc in path.read_text().split("\n---\n"):
            if "components/envoy-gateway-oidc" not in doc:
                continue
            lines = doc.splitlines()
            name = next((m.group(1) for l in lines if (m := NAME_RE.match(l))), None)
            app = next((m.group(1) or name for l in lines if (m := APP_RE.match(l))), name)
            if not app:
                sys.exit(f"ERROR: {path}: cannot resolve APP for the envoy-gateway-oidc component")
            refs += [(path, f"{app}-oidc", "client-id"), (path, f"{app}-oidc", "client-secret")]
    return refs


def manifest_refs(root: Path = Path("kubernetes")) -> list[tuple[Path, str, str]]:
    """(file, item, field) for every remoteRef/extract key under kubernetes/; field may be ''.

    Only documents whose secretStoreRef is the onepassword/openbao ClusterSecretStore are
    considered. Handles both the `item/field` form and the post-rewrite `key` + `property` form."""
    refs = []
    for path in sorted(root.rglob("externalsecret.yaml")):
        for doc in path.read_text().split("\n---\n"):
            lines = doc.splitlines()
            if not any(STORE_RE.match(line) and lines[i - 1].strip() == "kind: ClusterSecretStore"
                       for i, line in enumerate(lines) if i > 0):
                continue
            for i, line in enumerate(lines):
                m = KEY_RE.match(line)
                if not m or i == 0 or lines[i - 1].strip() not in KEY_PARENTS:
                    continue
                key = m.group(1)
                if "${" in key:  # substituted per app; see component_refs()
                    continue
                item, _, field = key.partition("/")
                if not field and i + 1 < len(lines) and (pm := PROP_RE.match(lines[i + 1])):
                    field = pm.group(1)
                refs.append((path, item, field))
    return refs + component_refs(root)


def copy(bao: Bao, titles: list[str]) -> int:
    """Copy the items kubernetes/ references; everything else in the vault (bootstrap
    secrets for etincelle, Talos, 1Password itself) stays where it is."""
    wanted = sorted({item for _, item, _ in manifest_refs()})
    missing = [t for t in wanted if t not in titles]
    if missing:
        sys.exit(f"ERROR: referenced in kubernetes/ but not in 1Password vault {OP_VAULT}: {missing}")
    for title in wanted:
        fields = item_fields(title)
        if not fields:
            sys.exit(f"ERROR: {title}: no labelled fields or files to copy")
        bao.kv_put(title, fields)
        print(f"--> {title}: {len(fields)} field(s)")
    print(f"==> Copied {len(wanted)} of {len(titles)} items (the rest are not referenced by kubernetes/). "
          f"Run '{sys.argv[0]} verify' next.")
    if BASE64_ITEMS:
        flags = " ".join(f"--base64 {t}" for t in sorted(BASE64_ITEMS))
        print(f"    Binary files were stored base64-encoded; run the manifest rewrite with: {flags}")
    return 0


def verify(bao: Bao, titles: list[str]) -> int:
    ok = True
    refs = manifest_refs()
    for title in sorted({item for _, item, _ in refs}):
        if title not in titles:
            print(f"MISSING  {title}  (not in 1Password)")
            ok = False
            continue
        want = item_fields(title)
        have = bao.kv_get(title)
        if have is None:
            print(f"MISSING  {title}")
            ok = False
        elif have != want:
            bad = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
            print(f"DIFFERS  {title}: {', '.join(bad)}")
            ok = False

    if BASE64_ITEMS:
        print(f"    (base64-encoded in OpenBao: {sorted(BASE64_ITEMS)}; their ExternalSecrets need decodingStrategy: Base64)")
    print("==> Checking every remoteRef in kubernetes/ resolves in OpenBao...")
    cache: dict[str, dict[str, str] | None] = {}
    for path, item, field in refs:
        if item not in cache:
            cache[item] = bao.kv_get(item)
        data = cache[item]
        if data is None:
            print(f"MISSING  {item}  ({path})")
            ok = False
        elif field and field not in data:
            print(f"NOFIELD  {item}/{field}  ({path})")
            ok = False
    print("==> OK: OpenBao matches 1Password and every reference resolves." if ok else "==> FAILED")
    return 0 if ok else 1


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else None
    if mode not in ("copy", "verify"):
        sys.exit(f"Usage: {sys.argv[0]} copy|verify")
    token = os.environ.get("BAO_TOKEN") or sys.exit("ERROR: BAO_TOKEN is required")
    bao = Bao(os.environ.get("BAO_ADDR", "https://bao.etincelle.cloud"), token)
    try:
        titles = item_titles()
        print(f"==> {len(titles)} items in 1Password vault {OP_VAULT}")
        return copy(bao, titles) if mode == "copy" else verify(bao, titles)
    except subprocess.CalledProcessError as e:
        sys.exit(f"ERROR: {' '.join(e.cmd[:3])} failed: {e.stderr.strip()}")


if __name__ == "__main__":
    sys.exit(main())
