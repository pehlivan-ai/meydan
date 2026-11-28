"""Deprecated: meydan.events is now meydan.bus (0.56.0). `Events` is `Bus`, `Event` is
`DomainEvent`. This module goes in 0.57.0, when meydan.events becomes organized events (an
occasion with organizers, participation and a program); import from meydan.bus."""

import warnings

from meydan.bus import *  # noqa: F403 (the deprecated names, all of them)
from meydan.bus import Bus as Events  # noqa: F401
from meydan.bus import DomainEvent as Event  # noqa: F401

warnings.warn(
    "meydan.events is now meydan.bus (Events -> Bus, Event -> DomainEvent); "
    "meydan.events becomes organized events in 0.57.0",
    DeprecationWarning,
    stacklevel=2,
)
