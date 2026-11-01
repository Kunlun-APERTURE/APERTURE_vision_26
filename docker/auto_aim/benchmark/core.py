from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from pathlib import Path
import hashlib
import json


def sha(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def identity(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


@dataclass(frozen=True)
class ScenarioPlan:
    scene: str
    version: int
    script: str
    metadata: dict

    def manifest(self):
        data=asdict(self)
        return dict(data,sha256=identity(data))


class Scenario(ABC):
    tier='basic'
    version=1

    def validate(self,parameters):
        if parameters:raise ValueError('This scene has no configurable parameters')
        return parameters

    @abstractmethod
    def generate(self, seed: int) -> ScenarioPlan: ...

    def difficulty(self, folder, output, shots):
        return dict(applicable=False)


class AlgorithmAdapter(ABC):
    @abstractmethod
    def build(self, root: Path, output: Path) -> Path: ...

    @abstractmethod
    def describe(self, root: Path, artifact: Path) -> dict: ...


@dataclass(frozen=True)
class EnvironmentSpec:
    id: str='E1'
    gimbal_dynamics: str='rm2017-yaw-corrected-raw'
    timing_profile: str='ui-aligned-v7-clock'

    def settings(self,scene,seed,trajectory_seed):
        return dict(scene=scene,terrain='flat_range',mode='shoot',matrix='single',
            gimbal_profile='feedforward',gimbal_dynamics=self.gimbal_dynamics,
            physics='no-drag',fire_policy='advice',fire_rate_hz=5.,bullet_speed=25.,reported_bullet_speed=25.,
            aim_origin='muzzle',projectile_visible='on',max_fps=240,algorithm_lifecycle='fresh',
            stage='final',evidence='full',execution_mode='lockstep',timing_profile=self.timing_profile,
            record_timing='off',detector_history='session',session_evidence='on',input_contract='aligned-v1',
            sensor_seed=seed,warmup_frames=30,seed=trajectory_seed,label='benchmark-'+self.id)


@dataclass(frozen=True)
class ExperimentSpec:
    algorithm: str
    environment: str
    scenes: tuple
    sensor_seeds: tuple
    trajectory_seed: int=2026102207
    repeats: int=1
    renderer: str='auto'

    def validate(self):
        if self.renderer not in ('auto','cpu','nvidia'):raise ValueError('Unknown renderer')
        if not self.scenes or not self.sensor_seeds or not 1<=self.repeats<=10:raise ValueError('Empty selection or invalid repeat count')
        if len(set(self.sensor_seeds))!=len(self.sensor_seeds):raise ValueError('Duplicate sensor seeds; use repeats')
        for seed in (*self.sensor_seeds,self.trajectory_seed):
            if type(seed) is not int or not 0<=seed<=4294967295:raise ValueError('Seed must be uint32')
        return self
