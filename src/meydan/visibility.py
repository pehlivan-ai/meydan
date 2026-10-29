"""Visibility: which parts of the data a signed-in person may see (docs/visibility.md).

Access (roles, permissions) answers "what may this person do"; visibility turns that into "what
does this person see". Each sensitive part of the data is one item with a rule: the scope that
unlocks it, what everyone else gets instead (hidden or masked), and whether each read is audited.
The read models apply the rules inside their queries, so a screen can't show more than allowed.

Two presets, then override any item:

    Visibility.need_to_know()      # default: each item needs its own scope
    Visibility.trusted("ops")      # everyone with the console's scope sees everything
    Visibility.need_to_know(addresses=Rule("messages:read", audit=True, otherwise="mask"))

Reads stay audited in both presets: trusting the team is not the same as not knowing who looked.
"""

from dataclasses import dataclass, fields, replace
from typing import Literal

from meydan.auth.core import Principal


@dataclass(frozen=True)
class Rule:
    """Who sees an item: people with `scope`. Everyone else gets it hidden (None) or masked."""

    scope: str
    audit: bool = False  # write each read to the audit table
    otherwise: Literal["hide", "mask"] = "hide"


@dataclass(frozen=True)
class Visibility:
    """One rule per item. A person always sees their own conversations, text included."""

    operations: Rule  # overview and problems: counts, health, errors
    all_conversations: Rule  # rows: everyone's conversations (otherwise only your own)
    message_text: Rule  # what people wrote and what the agent answered
    addresses: Rule  # phone numbers and other channel addresses
    agent_details: Rule  # per turn: model, tokens, timings, errors
    costs: Rule  # money: per turn, per conversation, reports

    @classmethod
    def need_to_know(cls, **overrides: Rule | str) -> "Visibility":
        """Least privilege: each item needs its own scope (map them to roles in your model)."""
        return cls(
            operations=Rule("ops"),
            all_conversations=Rule("conversations:read:all"),
            message_text=Rule("messages:read", audit=True),
            addresses=Rule("addresses:read", audit=True, otherwise="mask"),
            agent_details=Rule("ops"),
            costs=Rule("costs:read"),
        ).but(**overrides)

    @classmethod
    def trusted(cls, scope: str = "ops", **overrides: Rule | str) -> "Visibility":
        """A small trusted team: whoever holds `scope` sees everything (reads still audited)."""
        return cls(
            operations=Rule(scope),
            all_conversations=Rule(scope),
            message_text=Rule(scope, audit=True),
            addresses=Rule(scope, audit=True, otherwise="mask"),
            agent_details=Rule(scope),
            costs=Rule(scope),
        ).but(**overrides)

    def but(self, **overrides: Rule | str) -> "Visibility":
        """A copy with some items changed; a bare string keeps the item's audit/otherwise."""
        names = {f.name for f in fields(self)}
        changes: dict[str, Rule] = {}
        for name, value in overrides.items():
            if name not in names:
                raise ValueError(f"unknown visibility item {name!r}; items: {sorted(names)}")
            current: Rule = getattr(self, name)
            changes[name] = replace(current, scope=value) if isinstance(value, str) else value
        return replace(self, **changes)

    def sees(self, who: Principal, item: str) -> bool:
        return who.has(getattr(self, item).scope)


def mask(value: str) -> str:
    """+905321234567 -> +90 5•• ••• •• 67; opaque ids keep only their last 4 characters."""
    if value.startswith("+") and value[1:].isdigit():
        return value[:3] + " " + "•" * max(len(value) - 5, 0) + value[-2:]
    return "…" + value[-4:]


__all__ = ["Rule", "Visibility", "mask"]
