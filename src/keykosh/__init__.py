"""KeyKosh (K2) Python SDK — read configuration from your self-hosted platform."""

from .client import K2Client, create_client
from .config_store import K2ConfigStore
from .configuration import K2Configuration
from .errors import K2Error, K2ErrorCode
from .sts_signer import aws_sts_signer

__all__ = [
    "K2Client",
    "create_client",
    "K2Configuration",
    "K2ConfigStore",
    "K2Error",
    "K2ErrorCode",
    "aws_sts_signer",
]

__version__ = "1.1.0"
