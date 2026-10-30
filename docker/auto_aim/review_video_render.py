"""Offline renderer. Time and scores come from archived evidence, never MP4 FPS."""
import bisect
from functools import lru_cache
import html
import json
import math
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from review_video import SCENES, digest, frame_at, lines, load, require, shot_snapshot, shot_time, verify_inputs, write

W,H,FPS=2560,1440,30
BG=(24,20,16);PANEL=(38,32,26);WHITE=(238,234,226);MUTED=(178,167,151)
BEFORE=(231,165,90);AFTER=(143,218,67)
COLORS=((112,202,255),(255,156,166),(140,230,151))
FONT=None

@lru_cache(maxsize=512)
def text_tile(text,size,color):
    font=ImageFont.truetype(FONT,size,index=0)
    box=font.getbbox(text);im=Image.new('RGBA',(max(1,box[2]+4),size+12),(0,0,0,0))
    ImageDraw.Draw(im).text((0,-box[1]+3),text,font=font,fill=(*color[::-1],255))
    a=np.asarray(im);return a[:,:,:3][:,:,::-1].copy(),a[:,:,3:4]/255.

def text(canvas,text,x,y,size=24,color=WHITE):
    tile,alpha=text_tile(str(text),size,color);h,w=tile.shape[:2]
    h=min(h,canvas.shape[0]-y);w=min(w,canvas.shape[1]-x)
    if x<0 or y<0 or h<=0 or w<=0:return
    crop=canvas[y:y+h,x:x+w];a=alpha[:h,:w]
    crop[:]=np.rint(tile[:h,:w]*a+crop*(1-a)).astype(np.uint8)

def fmt(value,digits=1):
    return '—' if value is None else f'{value:.{digits}f}'

def pct(value):
    return '—' if value is None else f'{100*value:.1f}%'

def project(frame,points):
    camera=frame['camera_in_map'];x,y,z,w=np.asarray(camera['quaternion'],float)/np.linalg.norm(camera['quaternion'])
    r=np.asarray([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],[2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],[2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
    optical=(np.asarray(points)-camera['position'])@r
    if np.any(optical[:,2]<=.05):return None
    p=optical@np.asarray(frame['calibration']['k']).reshape(3,3).T
    return p[:,:2]/p[:,2,None]

class Evidence:
    def __init__(self,side,output):
        self.side=side;self.folder=Path(side['folder']);self.rows=list(lines(self.folder/'frames.jsonl'))
        self.times=[(r['stamp_ns']-side['start_ns'])*1e-9 for r in self.rows]
        self.stream=(self.folder/'input.mjpg').open('rb')
        self.annotated=cv2.VideoCapture(str(self.folder/'annotated.avi')) if (self.folder/'annotated.avi').exists() else None
        self.events={}
        for p in lines(self.folder/'session-evidence/events.jsonl'):
            if p['kind']=='result':self.events[p['data']['frame']['seq']]=p['data']
        self.gimbal=[p['data'] for p in lines(self.folder/'telemetry.jsonl') if p['kind']=='gimbal_trace']
        self.gimbal_times=[(p['stamp_ns']-side['start_ns'])*1e-9 for p in self.gimbal]
        self.plan=load(self.folder/'configuration/scenario-plan.json') or {}
        self.motion=load(self.folder/'motion-analysis.json') or {}
        self.shots=list(lines(output/'normalized-scores'/side['run_id']/'shots.jsonl'))
        self.last=-1;self.image=None;self.selections=[];self.gap_frames=0
        for r in self.rows:
            e=self.events.get(r['seq'])
            if e:
                fc=e['result'].get('fire_check') or {}
                r['fire_yaw_error_deg']=abs(math.degrees(fc['yaw_error_rad'])) if fc.get('yaw_error_rad') is not None else None
                r['fire_pitch_error_deg']=abs(math.degrees(fc['pitch_error_rad'])) if fc.get('pitch_error_rad') is not None else None
                r['selected_plate_error_m']=r.get('selected_plate_position_error_m')
                r['distance_m']=math.dist(r['ego_position'][:2],r['target_position'][:2]) if r.get('ego_position') and r.get('target_position') else None
        final=shot_snapshot(self.shots,side['tail_end_s'],side['start_ns'],side['measurement_s'],True)
        require((final['shots'],final['hits'])==(side['score']['shots_fired'],side['score']['target_armor_hits']),'Final score mismatch')
        require(abs((final['weighted'] or 0)*final['shots']-side['score']['weighted_hit_score_sum'])<1e-8,'Final weight mismatch')
        self.final=final

    def close(self):
        self.stream.close()
        if self.annotated:self.annotated.release()

    def camera(self,t):
        idx=frame_at(self.times,t)
        if idx<0:return None,None,None
        row=self.rows[idx];age=t-self.times[idx]
        if idx!=self.last:
            self.stream.seek(row['jpeg_offset']);blob=self.stream.read(row['jpeg_bytes'])
            require(len(blob)==row['jpeg_bytes'],'Truncated source JPEG')
            img=cv2.imdecode(np.frombuffer(blob,np.uint8),cv2.IMREAD_COLOR)
            require(img is not None,'JPEG decode failure')
            if self.annotated and self.annotated.isOpened():
                self.annotated.set(cv2.CAP_PROP_POS_FRAMES,row['video_index']);ok,marked=self.annotated.read()
                require(ok and marked.shape==img.shape,'Annotation index does not match raw archive')
                # Preserve exact archived overlays, remove its obsolete header; new score header is authoritative.
                marked[:112]=img[:112];img=marked
            elif row.get('drawings'):
                for name,args in row['drawings']:
                    require(name in ('polylines','putText','drawMarker','circle','rectangle'),'Unknown archived drawing operation')
                    if name=='polylines':args=[[np.asarray(p,np.int32) for p in args[0]],*args[1:]]
                    getattr(cv2,name)(img,*args)
                # The frozen recorder's header is removed, leaving its exact geometry overlays.
                self.stream.seek(row['jpeg_offset'])
                raw=cv2.imdecode(np.frombuffer(self.stream.read(row['jpeg_bytes']),np.uint8),cv2.IMREAD_COLOR)
                img[:112]=raw[:112]
            else:
                event=self.events.get(row['seq'])
                if event:
                    result=event['result'];frame=event['frame']
                    for obs in result.get('observations',[]):
                        points=np.asarray(obs.get('corners',[]))
                        if points.shape==(4,2):cv2.polylines(img,[np.rint(points).astype(np.int32)],True,(0,255,0),2)
                    aim=result.get('aim_point')
                    if aim:
                        uv=project(frame,[np.asarray(aim)+frame['gimbal_position_map']])
                        if uv is not None and np.isfinite(uv).all():cv2.circle(img,tuple(np.rint(uv[0]).astype(int)),7,(0,255,255),2)
            self.image=img;self.last=idx
        return self.image,row,age

    def snapshot(self,t,final=False):
        return shot_snapshot(self.shots,t,self.side['start_ns'],self.side['measurement_s'],final)

class Charts:
    SERIES=((('fire_yaw_error_deg','Yaw'),('fire_pitch_error_deg','Pitch')),
            (('center_xy_error_m','车体中心'),('selected_plate_error_m','选中装甲')),
            (('target_speed_mps','目标速度'),('target_yaw_rate','角速度')),
            (('distance_m','交战距离'),('ego_speed_mps','己方速度')))
    LABELS=('云台对准偏差 / °','跟踪位置误差 / m（离线真值）','目标运动 / m/s、rad/s（离线真值）','交战距离 / m、己方速度 / m/s')
    def __init__(self,left,right,duration):
        self.duration=duration;self.limits=[];self.cached=[]
        for group in self.SERIES:
            values=[abs(float(r[k])) for ev in (left,right) for stamp,r in zip(ev.times,ev.rows) for k,_ in group
                    if 0<=stamp<=ev.side['measurement_s'] and r.get(k) is not None and math.isfinite(r[k])]
            top=max(values,default=1.)
            signed=group==self.SERIES[2]
            self.limits.append((-top*1.12,top*1.12) if signed else (0,max(.01,top)*1.12))
        self.data={}
        for ev in (left,right):
            series=[]
            for group in self.SERIES:
                series.append([[(t,float(r[k])) for t,r in zip(ev.times,ev.rows)
                                if 0<=t<=ev.side['measurement_s'] and r.get(k) is not None and math.isfinite(r[k])] for k,_ in group])
            self.data[ev.side['run_id']]=series
    def draw(self,canvas,ev,t,x,y,width=850):
        for i,group in enumerate(self.SERIES):
            top=y+i*98;px=x+80;py=top+30;pw=width-105;ph=54;lo,hi=self.limits[i]
            text(canvas,self.LABELS[i],x,top,19)
            for j,(_,label) in enumerate(group):text(canvas,label,x+440+j*165,top,18,COLORS[j])
            for f in (0,.5,1):
                yy=round(py+ph*(1-f));cv2.line(canvas,(px,yy),(px+pw,yy),(66,60,51),1)
                text(canvas,fmt(lo+(hi-lo)*f,2),x,yy-10,16,MUTED)
            xx=px+round(pw*min(t,self.duration)/self.duration)
            cv2.line(canvas,(xx,py),(xx,py+ph),WHITE,1)
            for j,points in enumerate(self.data[ev.side['run_id']][i]):
                segments=[];current=[];last=None
                for stamp,value in points:
                    if stamp>t:break
                    if last is not None and stamp-last>.25:
                        if len(current)>1:segments.append(current)
                        current=[]
                    current.append((round(px+pw*stamp/self.duration),round(py+ph*(1-(value-lo)/(hi-lo)))))
                    last=stamp
                if len(current)>1:segments.append(current)
                for seg in segments:cv2.polylines(canvas,[np.asarray(seg,np.int32)],False,COLORS[j],2,cv2.LINE_AA)
            if i==len(self.SERIES)-1:
                for f in (0,.5,1):text(canvas,fmt(self.duration*f,1)+'s',round(px+pw*f)-15,py+ph+3,16,MUTED)

def target_plot(canvas,snap,t,start_ns,x,y,extent):
    text(canvas,'命中靶样 / 装甲中心归一化',x,y,20)
    cx,cy=x+173,y+167;rx,ry=125/extent,115/extent
    cv2.rectangle(canvas,(round(cx-rx),round(cy-ry)),(round(cx+rx),round(cy+ry)),(150,140,120),2)
    for f in (.25,.5,.75):
        cv2.rectangle(canvas,(cx-round(rx*f),cy-round(ry*f)),(cx+round(rx*f),cy+round(ry*f)),(72,68,59),1)
    cv2.line(canvas,(round(cx-rx),cy),(round(cx+rx),cy),(100,90,80),1)
    cv2.line(canvas,(cx,round(cy-ry)),(cx,round(cy+ry)),(100,90,80),1)
    for s in snap['impacts']:
        u,v=s['impact_x_fraction'],s['impact_y_fraction'];px=cx+round(rx*u);py=cy-round(ry*v)
        # Sphere edge contacts may lie just beyond the physical board: never clamp them into the board.
        color=COLORS[int(s.get('armor_id') or 0)%len(COLORS)]
        latest=s is snap['impacts'][-1]
        cv2.circle(canvas,(px,py),7 if latest else 4,color,2 if latest else -1,cv2.LINE_AA)
        if latest:
            text(canvas,'#'+str(s['shot_seq'])+' / 板'+str(s['armor_id']),x+10,y+296,19,color)
            if shot_time(s,start_ns) is not None and 0<=t-shot_time(s,start_ns)<.5:
                cv2.circle(canvas,(px,py),14,(255,255,255),2,cv2.LINE_AA)
    text(canvas,'不同装甲按各自中心叠加 / ±'+fmt(extent,1),x,y+330,17,MUTED)
    text(canvas,'无有效命中坐标：只计数，不造落点',x,y+355,16,MUTED)

def canvas_base():
    a=np.full((H,W,3),BG,np.uint8);cv2.line(a,(1280,60),(1280,H-40),(80,73,61),2)
    return a

def stats_text(s):
    return f'发射 {s["shots"]}   命中 {s["hits"]}   未中 {s["misses"]}   飞行中 {s["in_flight"]}'

def render_case_frame(case,left,right,charts,t,final=False):
    a=canvas_base();duration=case['play_s']
    text(a,SCENES.get(case['scene'],case['scene'])+'  |  种子 '+str(case['sensor_seed'])+'  |  '+case['engine']+'（单轮描述性）',30,12,28)
    text(a,'场景同步 t='+fmt(min(t,duration),2)+'s   /   原速 1×',1850,18,24)
    for column,ev in enumerate((left,right)):
        x=column*1280;side=ev.side;color=BEFORE if column==0 else AFTER
        title=('v3' if column==0 else 'R4') if case.get('reference_yaw_environment') else ('优化前  '+case.get('baseline_label','v3') if column==0 else '优化后  '+side['variant'])
        phase='计分 / 允许 advice' if t<=side['measurement_s'] else '停止开火 / 弹丸结算'
        if final:phase='本组最终结果'
        text(a,title,x+24,66,30,color);text(a,phase,x+650,72,23,MUTED)
        snap=ev.snapshot(t,final);text(a,stats_text(snap),x+24,112,23)
        text(a,'精确 '+pct(snap['exact'])+'    加权 '+pct(snap['weighted'])+'    命中/秒 '+fmt(snap['hits_per_s'],2),x+24,148,24,color)
        image,row,age=ev.camera(t)
        if image is not None:
            h,w=image.shape[:2];scale=min(1232/w,690/h);size=(round(w*scale),round(h*scale))
            view=cv2.resize(image,size,interpolation=cv2.INTER_AREA);ox=x+24+(1232-size[0])//2;oy=195+(690-size[1])//2
            if age<=.25:a[oy:oy+size[1],ox:ox+size[0]]=view
            else:text(a,'图像缺帧，停止显示旧画面',x+330,480,30,(100,120,255))
            if age>.1:text(a,'缺帧 / 上帧已 '+fmt(age*1000,0)+'ms',x+35,200,22,(90,155,255))
            if final:
                ev.selections.append(dict(output_t=t,source_index=row['video_index'],source_t=ev.times[ev.last],source_seq=row['seq'],held_final=True))
            else:
                ev.selections.append(dict(output_t=t,source_index=row['video_index'],source_t=ev.times[ev.last],source_seq=row['seq'],gap=age>.1))
                ev.gap_frames+=age>.1
            # Readable diagnostic inset, no changing the scale of the full view.
            event=ev.events.get(row['seq']);obs=event['result'].get('observations',[]) if event else []
            pts=[p for o in obs for p in o.get('corners',[])]
            if pts and age<=.25:
                points=np.asarray(pts);mid=np.mean(points,axis=0);bw=max(180,float(np.ptp(points[:,0]))*2);bh=bw*.65
                x0=max(0,min(w-round(bw),round(mid[0]-bw/2)));y0=max(112,min(h-round(bh),round(mid[1]-bh/2)))
                crop=image[y0:min(h,y0+round(bh)),x0:min(w,x0+round(bw))]
                if crop.size:
                    inset=cv2.resize(crop,(320,208));a[652:860,x+910:x+1230]=inset
                    cv2.rectangle(a,(x+909,651),(x+1231,861),(180,170,140),1);text(a,'局部放大（全景保持原比例）',x+906,864,16,MUTED)
            text(a,'帧 '+str(row['seq'])+'   取样 t='+fmt(ev.times[ev.last],3)+'s   '+str(row.get('tracker_state')),x+24,897,18,MUTED)
            if ev.plan.get('suite')=='movement-v1':
                stage=next((p for p in ev.plan.get('phases',[]) if p['t0']<=t<p['t1']),None)
                actual='实际 '+fmt(row.get('target_speed_mps'),2)+'m/s  '+fmt(row.get('target_yaw_rate'),2)+'rad/s  距离'+fmt(row.get('distance_m'),2)+'m'
                text(a,actual,x+24,923,17,WHITE)
                if stage:text(a,stage['name']+'  目标 '+fmt(stage['target_speed'],1)+'m/s / '+fmt(stage['target_omega'],1)+'rad/s',x+600,923,17,MUTED)
                difficulty=ev.motion.get('intensity_achieved')
                text(a,'运动难度：'+('已达标' if difficulty else '未达标/待核查')+'（整轮离线核验）',x+24,1332,18,AFTER if difficulty else (80,170,255))
        else:text(a,'等待首张带时间戳图像',x+360,480,28,MUTED)
        if case.get('reference_yaw_environment'):
            import bisect
            idx=bisect.bisect_right(ev.gimbal_times,t)-1
            text(a,case['reference_yaw_assumptions'],x+24,1355,19,MUTED)
            if idx>=0:
                g=ev.gimbal[idx];err=g.get('error_local_rad',[0])[0];target=g.get('target_yaw_rad',0)
                u=g.get('model_input_rad_s');v=g.get('model_output_rad_s')
                if case['reference_yaw_environment']=='legacy':
                    import tomllib
                    cfg=tomllib.loads((ev.folder/'configuration/simulator.toml').read_text());limit=cfg['vehicle']['gimbal_pid']['yaw']['max_rate']
                    u=max(-limit,min(limit,g['feedback_local_rad_s'][0]+g['feedforward_local_rad_s'][0]));v=u
                text(a,'目标 '+fmt(target,3)+' / 枪口 '+fmt(g.get('muzzle_yaw_before_control_rad'),3)+' / 误差 '+fmt(err,4)+'rad   角速 '+fmt(u,3)+' → '+fmt(v,3)+'rad/s',x+24,174,17,WHITE)
                text(a,'命令接收 '+str(g.get('accepted_wall_ns'))+' / 生效 '+str(g.get('model_first_effective_ns')),x+24,1380,16,MUTED)
        target_plot(a,snap,t,side['start_ns'],x+24,948,case['target_extent'])
        charts.draw(a,ev,min(t,side['measurement_s']),x+392,948)
    text(a,'绿色：检测   紫色：估计装甲   黄色：弹道瞄准点   |   曲线真值仅用于离线评估；未放宽开火窗口',25,1404,19,MUTED)
    return a

def title_card(spec,title,body):
    a=np.full((H,W,3),BG,np.uint8)
    text(a,title,100,110,54)
    for i,line in enumerate(body):text(a,line,105,240+i*72,30,MUTED if i>0 else WHITE)
    text(a,'优化前：'+spec.get('baseline_label','v3')+'（左）    优化后：'+spec['candidate']+'（右）',105,1180,34,AFTER)
    text(a,'视觉审查：待团队审查。自动规则通过不代替人工视觉验收。',105,1280,26,MUTED)
    return a

def summary_card(spec,index):
    a=np.full((H,W,3),BG,np.uint8);text(a,'独立后台统计 / 与 UI 单种子分开解释',65,55,43)
    text(a,'每格：精确命中 / 加权命中 / 命中每秒；同批同种子配对，重复性复跑不进入独立样本。',65,130,25,MUTED)
    labels={'all':'主场景合计',**SCENES};rows=spec['summary']
    if index==0:
        text(a,'场景 / 种子数',70,216,26);text(a,'优化前 '+spec.get('baseline_label','v3'),800,216,28,BEFORE);text(a,'优化后 '+spec['candidate'],1590,216,28,AFTER)
        y=280
        step=min(102,760/max(1,len(rows)))
        for row in rows:
            text(a,labels.get(row['scene'],row['scene'])+' / '+str(row['seeds'])+'种子',70,round(y),22 if len(rows)>8 else 26)
            for x,k,color in ((800,'before',BEFORE),(1590,'after',AFTER)):
                z=row[k];text(a,pct(z['target_hit_rate'])+' / '+pct(z['weighted_hit_rate'])+' / '+fmt(z['hits_per_s'],3),x,round(y),24 if len(rows)>8 else 29,color)
            y+=step
        qual=spec.get('qualification') or {};main=qual.get('main',[]) if isinstance(qual,dict) else []
        interval=next((x.get('weighted_gain_95') for x in main if x.get('reference')==spec['baseline']),None)
        if interval:text(a,'主场景加权收益配对95%区间：'+fmt(100*interval[0],2)+'～'+fmt(100*interval[1],2)+' 个百分点',70,1120,30,AFTER)
    else:
        body=['结论：'+('分档能力测评，本轮未修改算法；难度达标与射击效果分别判断' if spec.get('capability_study') else '独立效果与重复性已通过' if spec['acceptance_passed'] else '未通过完整验收，请检查报告失败项'),
              'UI 实录仅覆盖所列场景的预定种子，不能认证 UI 与后台统计等价。',
              '自车移动存在精确命中/吞吐或加权均值的小幅代价，完整数据保留。',
              '反转 UI 收益可能低于后台；固定逻辑时序不等于实车实时资格。',
              '短补偿参数和角点噪声近似需要实车重新标定，正式版本未自动替换。',
              '资料：章节、配对记录、源哈希、逐轮证据链接均随视频提供。']
        if spec.get('capability_study'):
            body=['结论：分档能力测评，本轮固定 v3 / '+spec['candidate']+'，没有修改算法。',
                  '实际运动难度与射击效果分别判断；以每组整轮离线难度状态为准。',
                  '0.8 / 0.4 秒变向受原伺服防抖与加速度约束，未达标项目单列。',
                  'UI 单种子与后台多种子分开解释，不构成引擎统计等价认证。',
                  '基础 / 进阶 / 压力分档选择；本次视频仅覆盖预定 UI 项目。',
                  '短补偿与角点噪声参数需实车重标定，正式版本未自动替换。']
        for i,s in enumerate(body):text(a,s,70,280+i*105,30,WHITE if i==0 else MUTED)
    text(a,'团队视觉验收状态：待审查',70,1320,32,WHITE)
    return a

class Encoder:
    def __init__(self,path):
        self.path=path;self.count=0
        self.process=subprocess.Popen(['ffmpeg','-nostdin','-loglevel','error','-y','-f','rawvideo','-pix_fmt','bgr24',
            '-s',f'{W}x{H}','-r',str(FPS),'-i','pipe:0','-an','-c:v','libx264','-threads','2','-preset','veryfast',
            '-crf','21','-pix_fmt','yuv420p','-movflags','+faststart',str(path)],stdin=subprocess.PIPE)
    def push(self,frame,n=1):
        blob=frame.tobytes()
        for _ in range(n):self.process.stdin.write(blob);self.count+=1
    def finish(self):
        self.process.stdin.close();require(self.process.wait()==0,'FFmpeg encoding failed')
    def abort(self):
        if self.process.poll() is None:self.process.kill();self.process.wait()

def encode_segment(path,frames,expected,code):
    if path.exists():
        p=probe(path)
        require(int(p['streams'][0]['nb_frames'])==expected,'Existing segment is incomplete')
        return
    tmp=path.with_name(path.stem+'.tmp.mp4');encoder=Encoder(tmp)
    try:
        frames(encoder);require(encoder.count==expected,'Wrong segment frame count');encoder.finish();tmp.replace(path)
    except BaseException:
        encoder.abort();tmp.unlink(missing_ok=True);raise
    print(json.dumps(dict(segment=path.name,frames=expected)),flush=True)

def probe(path):
    return json.loads(subprocess.check_output(['ffprobe','-v','error','-select_streams','v:0',
        '-show_entries','stream=codec_name,width,height,r_frame_rate,nb_frames,duration:format=duration',
        '-of','json',str(path)],text=True))

def export_page(output,spec,chapters):
    lis=[]
    for ch in chapters:
        start=ch['start_s'];mm,ss=divmod(int(start),60)
        lis.append('<li><button onclick="v.currentTime='+str(start)+';v.play()">'+f'{mm:02}:{ss:02} '+html.escape(ch['title'])+'</button></li>')
    links=[]
    for case in spec['cases']:
        for s in case['sides']:
            rel='../'+s['run_id']+'/report.html'
            links.append('<li>'+html.escape(SCENES.get(case['scene'],case['scene'])+' / '+s['variant'])+
                         ' <a href="'+rel+'">逐轮证据</a></li>')
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>优化前后视觉验收</title><style>body{background:#171b21;color:#e8edf3;font:18px/1.7 sans-serif;max-width:1450px;margin:30px auto;padding:0 24px}video{width:100%;max-height:80vh}a{color:#8bc5fa}button{background:#293746;color:white;border:1px solid #617386;padding:6px 15px;cursor:pointer}section{display:inline-block;vertical-align:top;width:48%}</style>
<h1>优化前 '''+html.escape(spec.get('baseline_label','v3'))+''' / 优化后 '''+html.escape(spec['candidate'])+'''</h1>
<p>左：优化前；右：优化后。完整计分与弹丸结算，原速播放。待团队视觉审查。</p>
<video id="v" controls preload="metadata" src="acceptance.mp4"></video>
<p><a download href="acceptance.mp4">下载主视频</a> · <a href="export.json">配对与参数</a> · <a href="validation.json">导出校验</a> · <a href="chapters.json">章节</a></p>
<section><h2>章节跳转</h2><ol>'''+''.join(lis)+'''</ol></section><section><h2>源记录</h2><ul>'''+''.join(links)+'''</ul></section>
<p>靶图为实际碰撞坐标按各装甲中心归一化；未命中不编造落点。真值曲线仅离线评估，未输入算法。UI单种子不等于统计等价认证。</p>
<p><a href="../'''+Path(spec['statistics']).name+'''/report.md">独立验收报告</a></p></html>'''
    (output/'report.html').write_text(page)
    (output/'report.md').write_text('# 优化前后视觉验收\n\n[播放视频](report.html) · [下载MP4](acceptance.mp4)\n\n左'+spec.get('baseline_label','v3')+'，右'+spec['candidate']+'；原速、完整计分与弹丸结算。靶图、命中计数和控制曲线按原始时间戳同步。待团队审查。\n')

def run(output):
    global FONT
    spec=load(output/'export.json');verify_inputs(spec);FONT=spec['font_path'];cv2.setNumThreads(1)
    media=output/'segments';media.mkdir(exist_ok=True);shots_dir=output/'screenshots';shots_dir.mkdir(exist_ok=True)
    chapters=[];segments=[];frame_total=0;audits=[];mapping={}
    def add(name,title,seconds,fn):
        nonlocal frame_total
        path=media/name;n=round(seconds*FPS);chapters.append(dict(title=title,start_s=frame_total/FPS,duration_s=n/FPS))
        encode_segment(path,fn,n,spec['code_sha256']);segments.append(path);frame_total+=n
    intro=title_card(spec,('参考 yaw 诊断 · '+spec['reference_yaw_environment']) if spec.get('reference_yaw_environment') else '优化前 / 优化后 视觉验收',[
        spec['engine']+' · 同场景 / 同种子 · 完整计分与结算，原速播放',
        '左侧固定 '+spec.get('baseline_label','v3')+'；右侧固定 '+spec['candidate']+'，不做 UI / 后台左右比较。',
        '画面下方：实际命中靶样、实时计数、云台偏差、位置误差和目标运动。',
        '独立后台多种子成绩在结尾汇总；单轮视频不替代统计验收。'])
    add('00-intro.mp4','说明',5,lambda enc:enc.push(intro,5*FPS));cv2.imwrite(str(shots_dir/'00-intro.png'),intro)
    for number,case in enumerate(spec['cases'],1):
        case=dict(case,engine=spec['engine'],baseline_label=spec.get('baseline_label','v3'),reference_yaw_environment=spec.get('reference_yaw_environment'),reference_yaw_assumptions=spec.get('reference_yaw_assumptions'))
        label=(spec.get('reference_yaw_assumptions','')+' / ' if spec.get('reference_yaw_environment') else '')+SCENES.get(case['scene'],case['scene'])+' / 种子 '+str(case['sensor_seed'])
        card=title_card(spec,label,[spec['engine']+' · 以计分起点 t=0 对齐',
            '完整计分 '+fmt(case['sides'][0]['measurement_s'],1)+' 秒 + 全部记录的弹丸结算',
            '计分结束后禁火；靶图继续显示飞行弹的实际碰撞结果。'])
        add(f'{number:02}-title.mp4',label+' / 说明',2,lambda enc,im=card:enc.push(im,2*FPS))
        left,right=[Evidence(side,output) for side in case['sides']]
        case['target_extent']=max([1.15]+[max(abs(s['impact_x_fraction']),abs(s['impact_y_fraction']))+.1 for ev in (left,right) for s in ev.shots if s.get('impact_x_fraction') is not None and s['useful_hit']])
        charts=Charts(left,right,max(ev.side['measurement_s'] for ev in (left,right)));n=math.ceil(case['play_s']*FPS)+1;path=media/f'{number:02}-comparison.mp4'
        chapters.append(dict(title=label+' / 完整对照',start_s=frame_total/FPS,duration_s=n/FPS))
        # Rebuild mapping deterministically even when an encoded segment is reused.
        def live(enc):
            for i in range(n):
                t=min(i/FPS,case['play_s'])
                image=render_case_frame(case,left,right,charts,t,final=i==n-1)
                enc.push(image)
                if i in (0,round(min(case['sides'][0]['measurement_s'],case['play_s'])*FPS/2),n-1):
                    cv2.imwrite(str(shots_dir/f'{number:02}-{i:04}.png'),image)
        if path.exists():
            path.unlink() # Derived pair media is safely regenerable; source experiments stay read-only.
        encode_segment(path,live,n,spec['code_sha256']);segments.append(path);frame_total+=n
        final_image=render_case_frame(case,left,right,charts,case['play_s'],True)
        add(f'{number:02}-final.mp4',label+' / 最终靶样',2,lambda enc,im=final_image:enc.push(im,2*FPS))
        for ev in (left,right):
            side=ev.side;mapping[side['run_id']]=ev.selections
            require(all(x['source_t']<=x['output_t']+1e-8 for x in ev.selections),'Future frame selected')
            final=ev.final;audits.append(dict(run_id=side['run_id'],final={k:v for k,v in final.items() if k!='impacts'},
                expected_shots=side['score']['shots_fired'],expected_hits=side['score']['target_armor_hits'],
                expected_weight=side['score']['weighted_hit_score_sum'],selected_frames=len(ev.selections),gap_output_frames=ev.gap_frames))
            ev.close()
    for i,duration in ((0,10),(1,5)):
        card=summary_card(spec,i);add(f'90-summary-{i}.mp4','独立统计' if i==0 else '结论与局限',duration,lambda enc,im=card,n=duration*FPS:enc.push(im,n))
        cv2.imwrite(str(shots_dir/f'90-summary-{i}.png'),card)
    write(output/'chapters.json',chapters);write(output/'frame-mapping.json',mapping);write(output/'score-audit.json',audits)
    concat=output/'concat.txt';concat.write_text(''.join("file '"+str(p)+"'\n" for p in segments))
    raw=output/'acceptance.raw.mp4'
    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-f','concat','-safe','0','-i',str(concat),'-c','copy',str(raw)],check=True)
    meta=output/'chapters.ffmetadata';content=';FFMETADATA1\ntitle=优化前 '+spec.get('baseline_label','v3')+' / 优化后 '+spec['candidate']+'\n'
    for c in chapters:content+='[CHAPTER]\nTIMEBASE=1/1000\nSTART='+str(round(c['start_s']*1000))+'\nEND='+str(round((c['start_s']+c['duration_s'])*1000))+'\ntitle='+c['title']+'\n'
    meta.write_text(content);tmp=output/'acceptance.tmp.mp4'
    subprocess.run(['ffmpeg','-nostdin','-loglevel','error','-y','-i',str(raw),'-i',str(meta),'-map_metadata','1','-map_chapters','1',
        '-c','copy','-movflags','+faststart',str(tmp)],check=True)
    tmp.replace(output/'acceptance.mp4');raw.unlink()
    info=probe(output/'acceptance.mp4');st=info['streams'][0]
    require((st['codec_name'],st['width'],st['height'],st['r_frame_rate'])==('h264',W,H,'30/1'),'Wrong video format')
    require(int(st['nb_frames'])==frame_total and abs(float(info['format']['duration'])-frame_total/FPS)<.1,'Wrong video duration')
    verify_inputs(spec)
    export_page(output,spec,chapters)
    write(output/'validation.json',dict(passed=True,width=W,height=H,fps=FPS,codec='h264',frames=frame_total,duration_s=frame_total/FPS,
        paired_cases=len(spec['cases']),source_hashes_unchanged=True,final_scores_match=True,no_future_camera_frames=True,
        complete_measurement_and_settlement=True,review_status='pending_team_review',ui_equivalence_certified=False,
        reproducibility='Frozen inputs/code/font and deterministic pairing/events/statistics. Not a claim of cross-FFmpeg byte-identical encoding.',
        video_sha256=digest(output/'acceptance.mp4')))
    names=['export.json','validation.json','acceptance.mp4','chapters.json','frame-mapping.json','score-audit.json','report.html','report.md']
    write(output/'export-seal.json',{n:digest(output/n) for n in names})
    print(json.dumps(dict(completed=True,output=str(output),duration_s=frame_total/FPS)),flush=True)

if __name__=='__main__':run(Path(sys.argv[1]).resolve())
