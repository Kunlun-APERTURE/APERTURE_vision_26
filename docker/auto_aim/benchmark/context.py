"""Runtime policy belongs to the caller, never the algorithm."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RunContext:
    root: Path
    source: str='local'
    request_id: str='local'

    def __post_init__(self):
        if self.source not in ('local','pr'):raise ValueError('Unknown experiment source')
        if not self.request_id or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in self.request_id):raise ValueError('Invalid request id')

    @property
    def publish_desktop(self):return self.source=='local'

    @property
    def retain_results(self):return self.source=='local'

    def workspace(self):return Path(self.root).resolve()/self.request_id
