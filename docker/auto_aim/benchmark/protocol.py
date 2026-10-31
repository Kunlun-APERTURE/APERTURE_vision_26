"""Public algorithm wire contract. Targets/truth belong to evaluation only."""
import math

PROTOCOL='rm26-image-v1'


def validate_result(frame,result):
    if any(result.get(k)!=frame.get(k) for k in ('session','seq','stamp_ns')):raise ValueError('Result does not match outstanding frame')
    if type(result.get('valid')) is not bool or type(result.get('fire_advice')) is not bool:raise ValueError('Boolean validity and advice required')
    if result['valid']:
        for key in ('yaw','pitch','distance'):
            value=result.get(key)
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value):raise ValueError('Invalid control '+key)
        if result['distance']<=0:raise ValueError('Positive range required')
    if result['fire_advice'] and not result['valid']:raise ValueError('Invalid command cannot advise firing')
    return result
