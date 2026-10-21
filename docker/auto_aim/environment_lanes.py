#!/usr/bin/env python3
"""Offline scene-authoring aid: rasterize static glTF obstacles and find clear lanes."""
import argparse,json,struct,math
from pathlib import Path
import cv2,numpy as np


def meshes(path):
    data=path.read_bytes();n,_=struct.unpack_from('<II',data,12);doc=json.loads(data[20:20+n]);offset=20+n;length,_=struct.unpack_from('<II',data,offset);binary=data[offset+8:offset+8+length]
    def array(index):
        a=doc['accessors'][index];b=doc['bufferViews'][a['bufferView']];count={'SCALAR':1,'VEC2':2,'VEC3':3,'VEC4':4,'MAT4':16}[a['type']]
        dtype=np.dtype({5126:'<f4',5125:'<u4',5123:'<u2',5121:'u1'}[a['componentType']]);stride=b.get('byteStride',dtype.itemsize*count)
        return np.ndarray((a['count'],count),dtype=dtype,buffer=binary,offset=b.get('byteOffset',0)+a.get('byteOffset',0),strides=(stride,dtype.itemsize)).copy()
    result=[]
    def visit(index,parent):
        node=doc['nodes'][index]
        if 'matrix' in node:local=np.array(node['matrix']).reshape(4,4).T
        else:
            x,y,z,w=node.get('rotation',[0,0,0,1]);R=np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)], [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)], [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
            local=np.eye(4);local[:3,:3]=R@np.diag(node.get('scale',[1,1,1]));local[:3,3]=node.get('translation',[0,0,0])
        M=parent@local
        if 'mesh' in node:
            for primitive in doc['meshes'][node['mesh']]['primitives']:
                if primitive.get('mode',4)!=4:continue
                points=array(primitive['attributes']['POSITION']);points=np.c_[points,np.ones(len(points))]@M.T;points=points[:,[2,0,1]]*[-1,-1,1]
                indices=array(primitive['indices']).reshape(-1) if 'indices' in primitive else np.arange(len(points))
                result.append(points[indices.reshape(-1,3)].astype(np.float32))
        for child in node.get('children',[]):visit(child,M)
    for index in doc['scenes'][doc.get('scene',0)]['nodes']:visit(index,np.eye(4))
    return np.concatenate(result)


def main():
    p=argparse.ArgumentParser();p.add_argument('--assets',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args();a.output.mkdir(parents=True,exist_ok=True)
    triangles=np.concatenate([meshes(a.assets/name) for name in ('GROUND.glb','OUTPOST.glb','TECH_CORE.glb','POWER.glb')])
    scale=50;mask=np.zeros((1401,751),np.uint8)
    for triangle in triangles:
        # The nominal flat floor is z=-0.2486; protrusions above -0.15 can
        # obstruct the low armour and cannot be used as flat driving lanes.
        if triangle[:,:,] .max(axis=0)[2]<-.15:continue
        uv=np.c_[(triangle[:,0]+7.5)*scale,(14-triangle[:,1])*scale]
        cv2.fillConvexPoly(mask,np.rint(uv).astype(np.int32),255)
    margin=20;mask=cv2.dilate(mask,cv2.getStructuringElement(cv2.MORPH_ELLIPSE,(2*margin+1,2*margin+1)))
    mask[:margin]=mask[-margin:]=255;mask[:,:margin]=mask[:,-margin:]=255
    distance=cv2.distanceTransform(255-mask,cv2.DIST_L2,5)
    candidates=[]
    for x in np.arange(-6.5,6.6,.5):
      for y in np.arange(-13,13.1,.5):
       for heading in (0,45,90,135,180,225,270,315):
        direction=np.array([math.cos(math.radians(heading)),math.sin(math.radians(heading))]);points=np.array([x,y])+np.arange(0,8.01,.05)[:,None]*direction
        uv=np.rint(np.c_[(points[:,0]+7.5)*scale,(14-points[:,1])*scale]).astype(int)
        if np.any(uv<0) or np.any(uv[:,0]>=mask.shape[1]) or np.any(uv[:,1]>=mask.shape[0]):continue
        clearance=distance[uv[:,1],uv[:,0]]
        if clearance.min()>0:candidates.append(dict(ego=[float(x),float(y)],target=points[-1].tolist(),heading_deg=heading,extra_clearance_m=float(clearance.min()/scale)))
    candidates.sort(key=lambda r:-r['extra_clearance_m']);image=cv2.cvtColor(255-mask,cv2.COLOR_GRAY2BGR)
    for lane in candidates[:10]:
        uv=np.array([[round((p[0]+7.5)*scale),round((14-p[1])*scale)] for p in (lane['ego'],lane['target'])]);cv2.line(image,tuple(uv[0]),tuple(uv[1]),(0,120,255),2)
    cv2.imwrite(str(a.output/'environment-lanes.png'),image)
    (a.output/'environment-lanes.json').write_text(json.dumps(dict(triangles=len(triangles),candidates=candidates[:30],robot_margin_m=.4,protrusion_threshold_z_m=-.15,
       limitations=['Conservative top-down rendering-mesh footprint; elevated objects can be overexcluded.','Scene-authoring only; no runtime algorithm input.','Candidate lane still requires a rendered and physical preflight.']),indent=2));print(json.dumps(dict(triangles=len(triangles),candidates=candidates[:5])))

if __name__=='__main__':main()
