import os
from pathlib import Path
import struct
import tempfile
import threading
import time
import unittest
from bridge.native_watch import DirectoryWatch, parse_notifications


class NativeParserTests(unittest.TestCase):
    def record(self,name,action=1,next_offset=0):
        data=name.encode('utf-16le')
        return struct.pack('<III',next_offset,action,len(data))+data

    def test_unicode_relative_path(self):
        self.assertEqual(list(parse_notifications(self.record('电影\\A.mkv'))),['电影/A.mkv'])

    def test_rename_pair(self):
        first=self.record('old.mkv',4);size=(len(first)+3)//4*4
        first=self.record('old.mkv',4,size)+b'\0'*(size-len(first))
        self.assertEqual(list(parse_notifications(first+self.record('new.mkv',5))),['old.mkv','new.mkv'])

    def test_invalid_records(self):
        for b in (b'x',self.record('../outside'),self.record('C:\\outside'),self.record('a',1,1),struct.pack('<III',0,1,123)):
            with self.subTest(data=b),self.assertRaises(ValueError):list(parse_notifications(b))


@unittest.skipUnless(os.name=='nt','Windows API required')
class WindowsWatchTests(unittest.TestCase):
    def test_real_create_notification_and_cancellation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();received=[];changed=threading.Event()
            def callback(library,scope):
                received.append((library,scope))
                if scope=='Film':changed.set()
            watcher=DirectoryWatch({'id':'test','source':str(root)},callback,lambda *_:None)
            watcher.start()
            try:
                self.assertTrue(watcher.ready_event.wait(10),'native watcher did not subscribe')
                (root/'Film').mkdir()
                (root/'Film'/'Movie.mkv').write_bytes(b'test')
                self.assertTrue(changed.wait(10),'no recursive file-create event received')
                self.assertIn(('test','Film'),received)
            finally:
                watcher.stop()
            self.assertFalse(watcher.thread.is_alive(),'watcher did not cancel')


if __name__=='__main__':unittest.main()
