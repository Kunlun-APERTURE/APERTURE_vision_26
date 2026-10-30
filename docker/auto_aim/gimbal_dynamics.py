"""Yaw reference identities/provenance; these fields never enter vision input."""
import hashlib
import json

HISTORICAL = 'rm2017-yaw-unitdc'
RAW = 'rm2017-yaw-corrected-raw'
UNITDC = 'rm2017-yaw-corrected-unitdc'
MODELS = ('legacy', HISTORICAL, RAW, UNITDC)
HISTORICAL_IMAGE = 'daedalus:rm26-reference-yaw-20261028'
CORRECTED_IMAGE = 'daedalus:rm26-reference-yaw-corrected-20261029'
FORMULA_SHA256 = '9b643c862db2ff3231b48af046cea839abed1ca06dec0c3d9a60ffd08b13b6bc'


def simulator_image(models, default):
    """One corrected runtime supports legacy and all reference IDs in a matrix."""
    models = (models,) if isinstance(models, str) else tuple(models)
    if any(model not in MODELS for model in models):
        raise ValueError('Unknown gimbal dynamics')
    if any(model in (RAW, UNITDC) for model in models):
        return CORRECTED_IMAGE
    return HISTORICAL_IMAGE if HISTORICAL in models else default


def description(model):
    if model not in MODELS:
        raise ValueError('Unknown gimbal dynamics')
    if model == 'legacy':
        return None
    historical = model == HISTORICAL
    denominator = [1, 43.16, 3975] if historical else [1, 34.16, 975]
    coefficients = dict(numerator=[-12.65, 1084], denominator=denominator)
    canonical = json.dumps(coefficients, sort_keys=True, separators=(',', ':'))
    normalized = model != RAW
    return dict(
        name=model, **coefficients,
        coefficient_sha256=hashlib.sha256(canonical.encode()).hexdigest(),
        original_dc_gain=1084 / denominator[2],
        applied_scale=denominator[2] / 1084 if normalized else 1.0,
        effective_dc_gain=1.0 if normalized else 1084 / denominator[2],
        normalization='G(s)/G(0); unit DC is a simulation assumption' if normalized else 'none; published velocity transfer function gain retained',
        source_status='transcription_error_historical_only' if historical else 'corrected_against_original_formula_image',
        axis='yaw',
        input='runtime: commanded yaw rate rad/s after simulator outer position PID and feedforward',
        source_interface='published target yaw rate to measured gyro yaw rate, with an existing inner speed PID; source export-unit conversion not independently verified',
        output='actual angular velocity rad/s before angle integration; final output rate limiter retained',
        discretization='exact ZOH state and linear angle integral; clipped intervals integrated to absolute tolerance 1e-12',
        source='https://bbs.robomaster.com/article/3559?source=5',
        source_formula='https://hz-rm-bbs-web-prod.oss-cn-hangzhou.aliyuncs.com/attachment/pre/v1/201710/11/213630m5dydtcgrahuolud.png',
        source_formula_sha256=FORMULA_SHA256,
        limitation=('Wrong denominator preserved for historical reproduction; not the published RM2017 model. ' if historical else '') +
            'Source is an identified closed-loop velocity response, not this robot calibration. Existing outer position PID/feedforward and final clipping are simulator assumptions; pitch uses the legacy model.',
        erratum='experiments/realism-audit-20261029/report.md')
