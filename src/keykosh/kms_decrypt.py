"""Decrypt a K2_TOKEN_ENC envelope via AWS KMS (design §1b/§1c).

The env var holds only ciphertext; the plaintext token never leaves memory. Cloud-only — requires
the optional ``boto3`` dependency and an ambient principal allowed ``kms:Decrypt``.
"""

from __future__ import annotations

import base64

from .errors import K2Error


def kms_decrypt(ciphertext_base64: str) -> str:
    if not ciphertext_base64 or not str(ciphertext_base64).strip():
        raise K2Error("K2_TOKEN_ENC is blank")
    try:
        import boto3  # type: ignore
    except ImportError as e:
        raise K2Error(
            "K2_TOKEN_ENC is set but the AWS KMS runtime is missing — install the optional "
            "dependency boto3 (this is a cloud-only credential; off-cloud, use a plaintext "
            "K2_TOKEN). See SDK_AUTH_AND_OFFLINE_DESIGN.md §1c."
        ) from e
    try:
        blob = base64.b64decode(str(ciphertext_base64).strip())
        client = boto3.client("kms")  # default region + credential provider chain
        resp = client.decrypt(CiphertextBlob=blob)
        return resp["Plaintext"].decode("utf-8").strip()
    except Exception as e:  # noqa: BLE001
        raise K2Error(
            f"K2_TOKEN_ENC could not be decrypted via AWS KMS: {e} — check the workload's IAM "
            "kms:Decrypt permission and AWS_REGION.", -1, e
        ) from e
