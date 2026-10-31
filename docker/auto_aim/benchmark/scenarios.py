from dataclasses import dataclass
from .core import Scenario, ScenarioPlan
from movement_scenarios import select, NEW_SCENES
from scenario_catalog import generated, SCENES
from pathlib import Path
import re
from highspeed_scenarios import DURATIONS


@dataclass(frozen=True)
class RegisteredScenario(Scenario):
    id: str
    version: int=1

    def generate(self,seed):
        if self.id=='ego_right_centered':
            text=(Path(__file__).parents[1]/'scenarios'/f'{self.id}.toml').read_text()
            text=re.sub(r'(?m)^duration\s*=.*$', 'duration = 8.0',text)
            text=re.sub(r'(?m)^seed\s*=.*$',f'seed = {seed}',text)
            text=re.sub(r'(?m)^t1\s*=\s*([0-9.]+)',lambda m:'t1 = '+str(min(float(m[1]),8.)),text)
            plan=dict(scene=self.id,seed=seed,duration_s=8.)
        else:text,plan=generated(self.id,seed,DURATIONS.get(self.id,6.))
        return ScenarioPlan(self.id,self.version,text,plan)

    def difficulty(self,folder,output,shots):
        if self.id not in NEW_SCENES:return super().difficulty(folder,output,shots)
        from movement_analysis import analyze
        return analyze(folder,output,shots)


REGISTRY={name:RegisteredScenario(name) for name in SCENES if name in DURATIONS or name=='ego_right_centered'}


def get(name):
    if name not in REGISTRY:raise ValueError('Scene not registered for synchronous benchmark: '+name)
    return REGISTRY[name]


def selection(tier='basic',cases=None,pressure=False):
    if tier not in ('basic','advanced'):raise ValueError('Unknown tier')
    if cases:
        requested=cases.split(',') if isinstance(cases,str) else list(cases)
        custom=[name for name in requested if name in REGISTRY and not isinstance(REGISTRY[name],RegisteredScenario)]
        native=[name for name in requested if name not in custom]
        names=(select(tier,native,pressure) if native else [])+custom
    else:names=select(tier,None,pressure)
    for name in names:get(name)
    return list(dict.fromkeys(names))


def register(name,scenario):
    if name in REGISTRY:raise ValueError('Duplicate scenario ID: '+name)
    if not isinstance(scenario,Scenario):raise TypeError('Scenario subclass required')
    REGISTRY[name]=scenario


from .examples.scene import ExampleStatic
register("example_static",ExampleStatic())
