from typing import Protocol

from semantic_entry.core.access import Actor


class Authorizer(Protocol):
    def is_allowed(self, actor: Actor, permission: str) -> bool: ...
