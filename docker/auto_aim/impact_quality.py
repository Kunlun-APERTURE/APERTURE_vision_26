"""Independent, geometry-only impact scoring. No algorithm inputs or commands."""
import math

VERSION=1


def finite_vector(value,n):
    return isinstance(value,(list,tuple)) and len(value)==n and all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in value)


def impact_coordinates(point,event):
    if not event.get('impact_geometry_valid') or not finite_vector(point,3):return None
    center,right,up,size=(event.get(k) for k in ('plate_center','plate_right','plate_up','plate_size'))
    if not all(finite_vector(v,n) for v,n in ((center,3),(right,3),(up,3),(size,2))):return None
    if min(size)<=0 or any(abs(sum(x*x for x in axis)-1)>.002 for axis in (right,up)) or abs(sum(x*y for x,y in zip(right,up)))>.001:return None
    delta=[x-c for x,c in zip(point,center)]
    x=sum(a*b for a,b in zip(delta,right));y=sum(a*b for a,b in zip(delta,up))
    rho=max(abs(x)/(size[0]/2),abs(y)/(size[1]/2))
    return dict(impact_x_m=x,impact_y_m=y,impact_center_distance_m=math.hypot(x,y),
        impact_x_fraction=x/(size[0]/2),impact_y_fraction=y/(size[1]/2),
        impact_center_proximity=1-min(1,rho))


def quantile(values,p):
    values=sorted(v for v in values if v is not None)
    return values[max(0,math.ceil(len(values)*p)-1)] if values else None


def summarize(rows,enabled=True):
    missing=sum(r.get('outcome') in ('target_armor','other_robot') and r.get('impact_center_proximity') is None for r in rows)
    available=enabled and not missing
    useful=[r for r in rows if r.get('useful_hit')]
    score=sum(r['impact_center_proximity'] for r in useful) if available else None
    return dict(impact_quality_version=VERSION if enabled else None,impact_geometry_missing=missing if enabled else None,
        weighted_hit_score_sum=score,weighted_hit_rate=score/len(rows) if available and rows else None,
        useful_armor_hits=len(useful),useful_armor_hit_rate=len(useful)/len(rows) if rows else None,
        impact_center_distance_p50_m=quantile([r.get('impact_center_distance_m') for r in useful],.5) if available else None,
        impact_center_distance_p95_m=quantile([r.get('impact_center_distance_m') for r in useful],.95) if available else None,
        impact_x_bias_m=sum(r['impact_x_m'] for r in useful)/len(useful) if available and useful else None,
        impact_y_bias_m=sum(r['impact_y_m'] for r in useful)/len(useful) if available and useful else None)
