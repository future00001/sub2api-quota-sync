#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import zipfile
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

PLUGIN_ID = "com.hzyhz.sub2api-quota-sync"
KEY_ID = "hzyhz-sub2api-quota-sync-v1"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_or_create_key(path: Path) -> Ed25519PrivateKey:
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), password=None)
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    os.chmod(path, 0o600)
    return key


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--ui", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--key", type=Path, required=True)
    args = parser.parse_args()

    files = {
        "runtimes/linux-amd64/plugin": sha256(args.binary),
        "ui/index.html": sha256(args.ui),
    }
    manifest = {
        "schema_version": 1,
        "id": PLUGIN_ID,
        "name": "7d 订阅配额同步",
        "version": "0.4.0",
        "description": (
            "配置账号 7d 周期与订阅配额同步；误启用宿主绑定时提供兼容 HTTP 流式透传。"
        ),
        "author": "hzyhz",
        "requires": {
            "sub2api": ">=0.1.183-0",
            "recommended_sub2api_version": "0.1.183",
            "tested_sub2api_versions": ["0.1.183", "0.1.183-custom-r2"],
            "plugin_protocol": 1,
            "transport_api": 1,
            "ui_bridge": 1,
        },
        "capabilities": [
            {
                "id": "openai.oauth.outbound_transport.v1",
                "platform": "openai",
                "account_type": "oauth",
            }
        ],
        "runtimes": {"linux-amd64": {"path": "runtimes/linux-amd64/plugin"}},
        "ui": {"entrypoint": "ui/index.html"},
        "files": files,
    }
    manifest_raw = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    key = load_or_create_key(args.key)
    signature = {
        "algorithm": "ed25519",
        "key_id": KEY_ID,
        "signature": base64.b64encode(key.sign(manifest_raw)).decode("ascii"),
    }
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manifest.json", manifest_raw)
        archive.writestr(
            "signature.json",
            json.dumps(signature, separators=(",", ":")).encode("utf-8"),
        )
        runtime = zipfile.ZipInfo("runtimes/linux-amd64/plugin")
        runtime.external_attr = 0o755 << 16
        archive.writestr(runtime, args.binary.read_bytes())
        archive.write(args.ui, "ui/index.html")
    print(
        json.dumps(
            {
                "output": str(args.output),
                "sha256": sha256(args.output),
                "key_id": KEY_ID,
                "public_key_base64": base64.b64encode(public_raw).decode("ascii"),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
