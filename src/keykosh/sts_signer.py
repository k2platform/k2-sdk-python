"""AWS STS workload-identity signer (design §1a).

Signs ``sts:GetCallerIdentity`` with SigV4 from the workload's ambient AWS credentials, producing an
envelope k2 relays to AWS STS. No AWS credentials leave the process — only the signed request is
sent, and k2 never sees the secret key. Cloud-only: uses ``botocore`` (bundled with the optional
``boto3`` dependency) for credential resolution + SigV4 signing.
"""

from __future__ import annotations

import os
from typing import Any

from .errors import K2Error

_BODY = "Action=GetCallerIdentity&Version=2011-06-15"


def aws_sts_signer() -> dict[str, Any]:
    """Return ``{"url", "headers", "body"}`` — a signed GetCallerIdentity request.

    Header values are lists of strings (the server deserializes into ``Map<String, List<String>>``).
    """
    try:
        import botocore.session
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
    except ImportError as e:
        raise K2Error(
            "STS auth needs botocore — install the optional dependency boto3 (this is a cloud-only "
            "credential; off-cloud, use a plaintext K2_TOKEN). See SDK_AUTH_AND_OFFLINE_DESIGN.md §1c."
        ) from e

    session = botocore.session.get_session()
    creds = session.get_credentials()
    if creds is None:
        raise K2Error(
            "No ambient AWS credentials resolved for STS signing — run where a task/instance role, "
            "AWS_* env vars, or a profile is available."
        )
    region = (
        session.get_config_variable("region")
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
    )
    if not region:
        raise K2Error("No AWS region resolved for STS signing — set AWS_REGION.")

    url = f"https://sts.{region}.amazonaws.com/"
    request = AWSRequest(
        method="POST", url=url, data=_BODY,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    SigV4Auth(creds.get_frozen_credentials(), "sts", region).add_auth(request)
    headers = {name: [value] for name, value in dict(request.headers).items()}
    return {"url": url, "headers": headers, "body": _BODY}
