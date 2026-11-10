from dataclasses import dataclass


@dataclass(frozen=True)
class Actor:
    actor_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.actor_id, str) or not self.actor_id.strip():
            raise ValueError("actor_id должен быть непустой строкой.")


class PermissionDeniedError(PermissionError):
    pass
