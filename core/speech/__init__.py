"""Speech quality subsystem for Yinyue.

This package owns the data-driven identity/conduct/speech/delivery split:

* ``domain``  - immutable value objects shared by every layer.
* ``store``   - persistence adapters for personas, bindings, rules and audits.
* ``resolve`` - persona / audience / policy resolution.
* ``outbound``- the policy gate that every outbound action must pass.
* ``facade``  - single entry point used by agents and the scheduler.
* ``bootstrap`` - moves shipped seed data into the database on first run.
"""

from core.speech.facade import SpeechFacade, get_speech_facade
from core.speech.outbound import OutboundGateway, get_outbound_gateway
from core.speech.store import SpeechStore, get_speech_store

__all__ = [
    "SpeechFacade",
    "get_speech_facade",
    "OutboundGateway",
    "get_outbound_gateway",
    "SpeechStore",
    "get_speech_store",
]
