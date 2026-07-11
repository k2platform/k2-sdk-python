"""KeyKosh (K2) Python SDK — read configuration from your self-hosted platform."""

from .client import K2Client, create_client
from .config_file_source import K2ConfigFileSource
from .configuration import K2Configuration
from .errors import K2Error
from .offline_cache import OfflineConfigCache
from .sts_signer import aws_sts_signer

__all__ = [
    "K2Client",
    "create_client",
    "K2Configuration",
    "K2ConfigFileSource",
    "K2Error",
    "OfflineConfigCache",
    "aws_sts_signer",
]

__version__ = "1.0.0"
