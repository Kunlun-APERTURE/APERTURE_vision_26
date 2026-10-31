import json
from pathlib import Path
from .algorithms import get
from .artifacts import export_bundle,import_bundle,verify


def add_parser(sub):
    p=sub.add_parser('algorithm',help='Build and verify independent algorithm artifacts')
    p.add_argument('operation',choices=('build','verify','export','import','report','compare','qualify','video','ui-check'))
    p.add_argument('--algorithm',default='B1');p.add_argument('--artifact',type=Path)
    p.add_argument('--bundle',type=Path);p.add_argument('--output',type=Path);p.add_argument('--images',action='store_true')
    p.add_argument('--candidate',type=Path);p.add_argument('--reference',type=Path)


def execute(args):
    import manage
    output=args.output or manage.OPS/'.runtime/benchmark/B1'
    required={'verify':('bundle',),'export':('bundle','output'),'import':('bundle','output'),
        'report':('output',),'compare':('output','candidate'),'qualify':('output',),'ui-check':('bundle','output'),'video':('reference','candidate','output')}
    for name in required.get(args.operation,()):
        if getattr(args,name) is None:raise ValueError('--'+name+' required for '+args.operation)
    if args.operation=='build':
        adapter=get(args.algorithm)
        artifact=adapter.build(manage.ROOT,output)
        result=adapter.describe(manage.ROOT,artifact)
        (output/'algorithm.json').write_text(json.dumps(result,indent=2)+'\n')
    elif args.operation=='verify':result=verify(args.bundle)
    elif args.operation=='export':result=export_bundle(args.bundle,args.output,args.images)
    elif args.operation=='import':result=str(import_bundle(args.bundle,args.output,args.images))
    elif args.operation=='report':
        from .evaluation import report
        result=report(args.output)
    elif args.operation=='qualify':
        from .qualification import qualify
        result=qualify(args.output)
    elif args.operation=='ui-check':
        from .ui import check
        result=check(args.bundle,args.output)
    elif args.operation=='video':
        from .video import prepare
        prepared=prepare(args.reference,args.candidate,args.output)
        from review_video import enqueue
        enqueue(prepared)
        result=str(prepared)
    else:
        from .evaluation import compare
        result=compare(args.output,args.candidate)
    print(json.dumps(result,indent=2))
