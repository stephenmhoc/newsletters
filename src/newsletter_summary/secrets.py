from __future__ import annotations

import os
import subprocess


def get_secret(env_name: str, keychain_service: str | None, keychain_account: str | None) -> str | None:
    value = os.environ.get(env_name)
    if value:
        return value
    if not keychain_service:
        return None
    command = ["security", "find-generic-password", "-s", keychain_service, "-w"]
    if keychain_account:
        command[2:2] = ["-a", keychain_account]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None

