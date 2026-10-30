"""Checks for independent projection and geometry interventions, not live algorithms."""
import copy
import math
import unittest
import cv2
import numpy as np

from perception_attribution_analysis import initial_geometry,solver_project


def fixture():
    frame=dict(q_gimbal2world=[0,0,0,1],gimbal_position_map=[0,0,0],calibration=dict(
        k=[1400,0,720,0,1400,540,0,0,1],d=[0]*5,
        r_camera2gimbal=[0,0,1,-1,0,0,0,-1,0],t_camera2gimbal=[0,0,0]))
    def armor(center,yaw,radius=.2,height=0.,index=0):
        xyz=np.asarray(center)-np.array([radius*math.cos(yaw),radius*math.sin(yaw),0.])+[0,0,height]
        corners=solver_project(frame,xyz,yaw)
        return dict(xyz=list(xyz),yaw=yaw,corners=corners.tolist(),confidence=.99-index*.01,
                    original_index=index,name='three',color='blue',type='small')
    return frame,armor


class ProjectionChecks(unittest.TestCase):
    def test_frontal_projection_uses_lightbar_height(self):
        f,_=fixture();p=solver_project(f,[6,0,0],0)
        self.assertTrue(np.isfinite(p).all())
        self.assertAlmostEqual(float(np.mean(p[:,0])),720,places=4)
        self.assertLess(float(np.ptp(p[:,1])),14)
        self.assertGreater(float(np.ptp(p[:,1])),10)

    def test_exact_four_plate_geometry_has_dual_support(self):
        f,a=fixture();o=[a([6,0,0],-.4,index=0),a([6,0,0],-.4+math.pi/2,index=1)]
        self.assertGreaterEqual(initial_geometry(f,o)['support'],2)

    def test_confidence_order_is_not_a_geometry_intervention(self):
        f,a=fixture();o=[a([6,0,0],-.4,index=0),a([6,0,0],-.4+math.pi/2,index=1)]
        s=initial_geometry(f,o)['support'];o.reverse()
        self.assertEqual(initial_geometry(f,o)['support'],s)

    def test_free_height_is_separate_from_radius(self):
        f,a=fixture();o=[a([6,0,0],-.4,index=0),a([6,0,0],-.4+math.pi/2,height=.08,index=1)]
        self.assertLess(initial_geometry(f,o)['support'],2)
        self.assertGreaterEqual(initial_geometry(f,o,heights=(-.08,0.,.08))['support'],2)

    def test_truth_yaw_intervention_does_not_replace_position(self):
        f,a=fixture();o=[a([6,0,0],-.4,index=0),a([6,0,0],-.4+math.pi/2,index=1)]
        truth={}
        for i,p in enumerate(o):
            y=p['yaw'];truth[i]=dict(plate=dict(position=p['xyz'],normal=[-math.cos(y),-math.sin(y),0]))
            p['yaw']+=.7
        before=copy.deepcopy(o)
        self.assertGreaterEqual(initial_geometry(f,o,truth=truth,truth_mode='yaw')['support'],2)
        self.assertEqual(before,o)

    def test_single_plate_cannot_be_called_dual_support(self):
        f,a=fixture();self.assertLess(initial_geometry(f,[a([6,0,0],-.4)])['support'],2)


if __name__=='__main__':unittest.main()
