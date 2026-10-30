"""Archive failure, history, immutability and corruption checks; no ROS needed."""
import json
from pathlib import Path
import queue
import tempfile
import unittest
from unittest.mock import patch

from session_evidence import SessionArchive,audit

JPEG=b'\xff\xd8\xff\xc0\x00\x0b\x08\x00\x02\x00\x03\x01\x01\x11\x00\xff\xd9'


class ArchiveChecks(unittest.TestCase):
    def write(self,root,missing=None,tail=False):
        archive=SessionArchive(root,'fresh')
        for n,stamp in enumerate((10,20,30)):
            frame=dict(session='fresh',seq=n,stamp_ns=stamp,calibration=dict(width=3,height=2))
            archive.record('input',frame,JPEG)
            if stamp!=missing:archive.record('result',dict(frame=frame,result=dict(tracker='tracking')))
            frame['seq']=-999  # The archive must own an immutable snapshot.
        if tail:
            archive.record('input',dict(session='fresh',seq=3,stamp_ns=50,calibration=dict(width=3,height=2)),JPEG)
        archive.close();archive.close()
        return audit(root,20,40)

    def test_complete_warmup_and_measurement(self):
        with tempfile.TemporaryDirectory() as root:
            result=self.write(root)
            self.assertTrue(result['passed'],result)
            self.assertEqual((result['inputs'],result['results'],result['warmup_inputs']),(3,3,1))

    def test_missing_warmup_or_measured_result_is_invalid(self):
        for stamp in (10,20,30):
            with self.subTest(stamp=stamp),tempfile.TemporaryDirectory() as root:
                self.assertFalse(self.write(root,missing=stamp)['passed'])

    def test_unfinished_post_measurement_tail_is_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            result=self.write(root,tail=True)
            self.assertTrue(result['passed'],result)
            self.assertEqual(result['unmatched_tail'],[['fresh',3,50]])

    def test_corrupt_or_truncated_images_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            self.write(root)
            image=Path(root)/'input.mjpg';image.write_bytes(image.read_bytes()[:-3])
            self.assertFalse(audit(root,20,40)['passed'])

    def test_queue_overflow_never_silently_passes(self):
        with tempfile.TemporaryDirectory() as root:
            archive=SessionArchive(root,'fresh')
            with patch.object(archive.pending,'put_nowait',side_effect=queue.Full):
                archive.record('input',{})
            archive.close()
            result=audit(root,20,40)
            self.assertFalse(result['passed'])
            self.assertTrue(any('Full' in error for error in result['errors']))

    def test_receipt_boundary_cannot_precede_warmup_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            archive=SessionArchive(root,'fresh')
            frame=dict(session='fresh',seq=1,stamp_ns=10,calibration=dict(width=3,height=2))
            archive.record('input',frame,JPEG)
            archive.record('result',dict(frame=frame,receipt_ns=25))
            archive.record('warmup_park',dict(stamp_ns=20,receipt_boundary=True,
                pipeline=dict(pending_command_receipt_ns=None)))
            archive.close();result=audit(root,20,40)
            self.assertFalse(result['passed'])
            self.assertIn('Warmup parked before final logical receipt',result['errors'])


if __name__=='__main__':unittest.main(verbosity=2)
