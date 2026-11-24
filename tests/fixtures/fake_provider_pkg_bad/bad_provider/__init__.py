"""Fake provider with a mismatched contract version (WP6 rejection test)."""

KIND = "bad_provider"
ADAPTER_CONTRACT_VERSION = 2


class BadProviderAdapter:
    name = "bad_provider"
    KIND = "bad_provider"
    ADAPTER_CONTRACT_VERSION = 2
