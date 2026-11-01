import hashlib
import json
import re
from dataclasses import dataclass,asdict

OPTIONS=('basic','variable','turn08','turn04','spin_startstop','pursuit','pressure')
SHA=re.compile(r'^[0-9a-f]{40}$')


def selection(body):
    chosen=[]
    for line in (body or '').splitlines():
        m=re.fullmatch(r'\s*- \[([ xX])\] experiment:([a-z0-9_]+)\s*',line)
        if not m:
            m=re.fullmatch(r'\s*- \[([ xX])\] [^<>]+?\s*<!--\s*experiment:([a-z0-9_]+)\s*-->\s*',line)
        if m:
            if m[2] not in OPTIONS+('none',):raise ValueError('Unknown experiment choice')
            if m[1].lower()=='x':chosen.append(m[2])
    if not chosen or chosen==['none']:return ()
    if 'none' in chosen:raise ValueError('none cannot accompany experiments')
    if 'pressure' in chosen and not set(chosen)&{'variable','pursuit'}:raise ValueError('Pressure requires variable or pursuit')
    return tuple(sorted(set(chosen)))


@dataclass(frozen=True)
class Request:
    repository: str
    number: int
    head: str
    base: str
    review_id: int
    reviewer: str
    choices: tuple
    attempt: int=0

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*',self.repository):raise ValueError('Invalid repository')
        if type(self.number) is not int or self.number<1 or not SHA.fullmatch(self.head) or not SHA.fullmatch(self.base):raise ValueError('Invalid PR identity')
        if not self.choices or set(self.choices)-set(OPTIONS):raise ValueError('Invalid choices')

    def data(self):return asdict(self)

    def key(self):
        # Multiple reviewers approving the same request do not duplicate work.
        d=self.data();d.pop('review_id');d.pop('reviewer')
        return hashlib.sha256(json.dumps(d,sort_keys=True).encode()).hexdigest()[:24]


def authorize(repository,pr,review,permission,body_changed_at=None):
    if pr['state']!='open' or pr.get('draft'):raise ValueError('PR must be open and ready')
    if review['state'].upper()!='APPROVED' or review['user']['login']==pr['user']['login']:raise ValueError('Independent approval required')
    if permission not in ('write','maintain','admin'):raise ValueError('Reviewer lacks write permission')
    if review['commit_id']!=pr['head']['sha']:raise ValueError('Approval is for an old commit')
    if body_changed_at and review['submitted_at']<body_changed_at:raise ValueError('Experiment choices changed after approval')
    return Request(repository,pr['number'],pr['head']['sha'],pr['base']['sha'],review['id'],review['user']['login'],selection(pr['body']))
