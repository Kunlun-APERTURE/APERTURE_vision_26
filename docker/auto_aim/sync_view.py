#!/usr/bin/env python3
"""Read-only UI: polling these published pixels cannot advance simulation."""
from pathlib import Path
import sys
import time
import cv2
import numpy as np


def main(directory):
    folder=Path(directory);last=None;started=time.monotonic();shown=False
    cv2.setNumThreads(1)
    while time.monotonic()-started<180:
        if (folder/'ended.json').exists():break
        try:
            path=folder/'latest.jpg';stamp=path.stat().st_mtime_ns
            if stamp!=last:
                data=path.read_bytes();frame=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
                if frame is not None:
                    cv2.putText(frame,'Synchronous simulation | read-only preview',(12,30),cv2.FONT_HERSHEY_SIMPLEX,.7,(0,255,255),2)
                    cv2.imshow('RM26 synchronous input',frame);last=stamp;shown=True
        except OSError:pass
        if shown and cv2.waitKey(20) in (ord('q'),27):break
        time.sleep(.02)
    cv2.destroyAllWindows()


if __name__=='__main__':main(sys.argv[1])
