#!/usr/bin/env python3
"""Independent protocol example: consumes frames, never authorizes firing."""
import argparse
import json
import socket
import struct
import time


def read(sock,n):
    value=b''
    while len(value)<n:
        block=sock.recv(n-len(value))
        if not block:raise EOFError('Bridge disconnected')
        value+=block
    return value


def send(sock,value):
    body=json.dumps(value).encode();sock.sendall(struct.pack('!II',len(body),0)+body)


def serve(sock):
    session=None
    while True:
        send(sock,dict(version=1,type='next'))
        n,payload=struct.unpack('!II',read(sock,8))
        if n>65536 or payload>16*1024*1024:raise ValueError('Oversized frame')
        frame=json.loads(read(sock,n));read(sock,payload)
        if frame['type']=='wait':continue
        result={k:frame[k] for k in ('session','seq','stamp_ns')}
        result.update(version=1,type='command',valid=False,fire_advice=False,yaw=0.,pitch=0.,distance=0.,detections=0,tracking=False)
        result.update(detector_history='session',detector_reset=session!=frame['session'])
        session=frame['session']
        send(sock,result)


def main():
    p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);args=p.parse_args()
    while True:
        try:
            with socket.create_connection(('127.0.0.1',args.port),timeout=30) as sock:serve(sock)
        except (OSError,EOFError):time.sleep(1)

if __name__=='__main__':main()
