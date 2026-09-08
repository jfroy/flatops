#!/usr/bin/env python3
"""Adopt generator-backed secrets into OpenBao, so a cluster rebuild cannot regenerate them.

Some generated values encrypt data at rest, or authenticate credentials that cannot be
reissued. Regenerating one is a silent data-loss event, and a rebuilt cluster regenerates
every `Password` generator by definition. This moves that set into the vault: the current
live value is written to `kantai/<item>`, and the ExternalSecret reads it with
`dataFrom.extract` instead of generating it.

Usage: scripts/openbao-adopt-generated.py seed [--force] [--create-missing]
       scripts/openbao-adopt-generated.py verify

  seed             read each live Secret with kubectl and write it to OpenBao
  --force          overwrite an item whose OpenBao content differs from the live Secret
  --create-missing mint a fresh value for an app that is not deployed and has no vault item
  verify           OpenBao matches every live Secret

Requires: python3 (stdlib only), kubectl with access to the cluster.
  BAO_ADDR   OpenBao address (default https://bao.etincelle.cloud)
  BAO_TOKEN  a token with create/update/read on kantai/* (`task openbao-token` in etincelle)

Secret values go from kubectl straight to OpenBao; nothing is written to disk.

`keys` lists the generator's own key names and the length `password<N>` produced, which is
what --create-missing mints. `mapper` turns the *rendered* Secret back into those key names,
so the ExternalSecret's template keeps working unchanged after the flip; `identity` means the
ExternalSecret has no template and the Secret already holds the generator's key names.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import string
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

KV_MOUNT = "kantai"
# generators.external-secrets.io Password password32/password64: symbols 0, upper allowed
ALPHABET = string.ascii_letters + string.digits


def identity(secret: dict[str, str]) -> dict[str, str]:
    return dict(secret)


def b64_field(rendered: str, raw: str):
    """The template applied `| b64enc`; recover the raw generator value."""
    return lambda secret: {raw: base64.b64decode(secret[rendered]).decode()}


def rename(mapping: dict[str, str]):
    """The template passed values through under other names: {rendered key: generator key}."""
    return lambda secret: {gen: secret[rendered] for rendered, gen in mapping.items()}


ADOPTIONS = [
    dict(ns="default", secret="homebox-keys", item="homebox-keys", mapper=identity,
         keys={"HBOX_AUTH_API_KEY_PEPPER": 32},
         why="pepper; rotating it invalidates every issued API key"),
    dict(ns="default", secret="pocket-id-keys", item="pocket-id-keys", mapper=identity,
         keys={"ENCRYPTION_KEY": 32},
         why="encrypts the token-signing private keys at rest"),
    dict(ns="default", secret="filebrowser-keys", item="filebrowser-keys", mapper=identity,
         keys={"FILEBROWSER_JWT_TOKEN_SECRET": 32, "FILEBROWSER_TOTP_SECRET": 32},
         why="TOTP secret encrypts stored TOTP secrets; changing it locks out every 2FA user"),
    dict(ns="default", secret="open-webui-keys", item="open-webui-keys", mapper=identity,
         keys={"WEBUI_SECRET_KEY": 32, "OAUTH_CLIENT_INFO_ENCRYPTION_KEY": 32,
               "OAUTH_SESSION_TOKEN_ENCRYPTION_KEY": 32},
         why="OAUTH_* keys encrypt stored OAuth credentials; a wrong key crashes startup"),
    dict(ns="litellm", secret="litellm-salt", item="litellm-salt",
         mapper=rename({"LITELLM_SALT_KEY": "SALT_KEY_RAW"}), keys={"SALT_KEY_RAW": 32},
         why="encrypts provider credentials in the LiteLLM database; upstream says never change it"),
    dict(ns="openshell", secret="openshell-cek", item="openshell-cek",
         mapper=b64_field("key-encryption-key", "KEK_RAW"), keys={"KEK_RAW": 32},
         why="wraps content encryption keys"),
    # DB_PASSWORD stays generated: postgres-init resets the role to whatever the Secret holds.
    dict(ns="observability", secret="kite", item="kite",
         mapper=rename({"JWT_SECRET": "JWT_SECRET", "KITE_ENCRYPT_KEY": "KITE_ENCRYPT_KEY"}),
         keys={"JWT_SECRET": 32, "KITE_ENCRYPT_KEY": 32},
         why="KITE_ENCRYPT_KEY encrypts stored data; JWT_SECRET shares its Secret"),
]


class Bao:
    def __init__(self, addr: str, token: str) -> None:
        self.addr, self.token = addr.rstrip("/"), token

    def request(self, method: str, path: str, data: dict | None = None) -> dict | None:
        body = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.addr}/v1/{path}", data=body, method=method,
                                     headers={"X-Vault-Token": self.token,
                                              "Content-Type": "application/json"})
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

    def get(self, item: str) -> dict[str, str] | None:
        resp = self.request("GET", f"{KV_MOUNT}/data/{urllib.parse.quote(item, safe='')}")
        return None if resp is None else resp["data"]["data"]

    def put(self, item: str, fields: dict[str, str]) -> None:
        self.request("POST", f"{KV_MOUNT}/data/{urllib.parse.quote(item, safe='')}", {"data": fields})


def live_secret(ns: str, name: str) -> dict[str, str] | None:
    """The rendered Secret, or None when the app is not deployed."""
    proc = subprocess.run(["kubectl", "get", "secret", "-n", ns, name, "-o", "json"],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        if "NotFound" in proc.stderr:
            return None
        sys.exit(f"ERROR: kubectl get secret -n {ns} {name}: {proc.stderr.strip()}")
    return {k: base64.b64decode(v).decode() for k, v in json.loads(proc.stdout).get("data", {}).items()}


def wanted(entry: dict) -> dict[str, str] | None:
    rendered = live_secret(entry["ns"], entry["secret"])
    if rendered is None:
        return None
    try:
        return entry["mapper"](rendered)
    except KeyError as e:
        sys.exit(f"ERROR: {entry['ns']}/{entry['secret']} has no key {e}; the ExternalSecret "
                 "template changed and this script's mapping is stale")


def mint(keys: dict[str, int]) -> dict[str, str]:
    return {k: "".join(secrets.choice(ALPHABET) for _ in range(n)) for k, n in keys.items()}


def seed(bao: Bao, force: bool, create_missing: bool) -> int:
    for e in ADOPTIONS:
        item, want, have = e["item"], wanted(e), bao.get(e["item"])
        if want is None:
            if have is not None:
                print(f"--> {item}: not deployed; OpenBao already holds a value")
            elif create_missing:
                bao.put(item, mint(e["keys"]))
                print(f"--> {item}: not deployed; minted {len(e['keys'])} value(s)")
            else:
                print(f"--> {item}: not deployed and no vault item. Re-run with --create-missing "
                      f"before enabling {e['ns']}/{e['secret']}.")
            continue
        if have == want:
            print(f"--> {item}: already current ({len(want)} field(s))")
        elif have is not None and not force:
            print(f"SKIP {item}: OpenBao content differs from the live Secret; --force to overwrite",
                  file=sys.stderr)
        else:
            bao.put(item, want)
            print(f"--> {item}: wrote {len(want)} field(s) from {e['ns']}/{e['secret']}")
    print("==> Seed done. Run 'verify', then merge the manifest change that drops the generators.")
    return 0


def verify(bao: Bao) -> int:
    ok = True
    for e in ADOPTIONS:
        item, want, have = e["item"], wanted(e), bao.get(e["item"])
        if want is None:
            if have is None:
                print(f"PENDING  {item}  (not deployed, no vault item; seed --create-missing)")
                ok = False
            elif sorted(have) != sorted(e["keys"]):
                print(f"SHAPE    {item}: vault holds {sorted(have)}, expected {sorted(e['keys'])}")
                ok = False
            else:
                print(f"OK       {item}  (not deployed; value held in OpenBao)")
        elif have is None:
            print(f"MISSING  {item}")
            ok = False
        elif have != want:
            bad = sorted(k for k in set(want) | set(have) if want.get(k) != have.get(k))
            print(f"DIFFERS  {item}: {', '.join(bad)}")
            ok = False
        else:
            print(f"OK       {item}  ({e['why']})")
    print("==> OpenBao matches every live Secret." if ok else "==> FAILED")
    return 0 if ok else 1


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else None
    if mode not in ("seed", "verify"):
        sys.exit(f"Usage: {sys.argv[0]} seed [--force] [--create-missing] | verify")
    token = os.environ.get("BAO_TOKEN") or sys.exit("ERROR: BAO_TOKEN is required")
    bao = Bao(os.environ.get("BAO_ADDR", "https://bao.etincelle.cloud"), token)
    if mode == "verify":
        return verify(bao)
    return seed(bao, "--force" in args, "--create-missing" in args)


if __name__ == "__main__":
    sys.exit(main())
