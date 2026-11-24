"""Fake provider used by WP6 entry-point tests (never talks to the network)."""

KIND = "fake_provider"
ADAPTER_CONTRACT_VERSION = 1


class FakeProviderAdapter:
    name = "fake_provider"
    KIND = "fake_provider"
    ADAPTER_CONTRACT_VERSION = 1
