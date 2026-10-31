from benchmark.core import Scenario,ScenarioPlan


class ExampleStatic(Scenario):
    tier='basic'
    version=1
    def generate(self,seed):
        text=f'''id = "rm26_example_static"
seed = {seed}
duration = 3.0
trigger = "on_load"
[[robot]]
id = 0
spawn = {{ x = 0.0, y = 0.0, z = 0.0, yaw_deg = 0.0 }}
[[robot.segment]]
t0 = 0.0
t1 = 3.0
type = "static"
[[robot]]
id = 1
spawn = {{ x = 5.0, y = 0.0, z = 0.0, yaw_deg = 180.0 }}
[[robot.segment]]
t0 = 0.0
t1 = 3.0
type = "static"
'''
        return ScenarioPlan('example_static',1,text,dict(duration_s=3.,seed=seed,semantics=dict(homing_yaw_deg=0.,ego_motion='static',target_motion='static',hit_evaluation='front_armor')))
