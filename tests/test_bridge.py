import copy
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from bridge.config import DEFAULT, MASK, atomic_json, redact, restore_secrets, validate
from bridge.covers import generate
from bridge.engine import Engine
from bridge.notify import deliver, map_path, send
from bridge.paths import safe_join
from bridge.paths import walk
from bridge.protocol import decode_change, fields, frame, messages, text_field, varint
from bridge.service import Service
from bridge.state import State
from bridge.transfer import Transfers, rename_noreplace


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source,self.output,self.data = [self.root/x for x in ('source','output','data')]
        self.source.mkdir(); self.data.mkdir()
        self.config = copy.deepcopy(DEFAULT)
        self.config['cloud']['enabled'] = False
        self.config['sync'].update(settle_seconds=0,delete_grace_seconds=0,max_delete_fraction=1,allow_empty_cleanup=True)
        self.lib = self.config['libraries'][0]
        self.lib.update(source=str(self.source),output=str(self.output),enabled=True)
        self.state = State(self.data/'state.sqlite3')
        self.engine = Engine(self.state,self.config)
        self.engine.initialize(self.lib)

    def tearDown(self):
        self.state.db.close()
        self.temp.cleanup()

    def file(self, name, content=b'video'):
        p = self.source/name; p.parent.mkdir(parents=True,exist_ok=True)
        p.write_bytes(content)
        os.utime(p,(time.time()-3600,time.time()-3600))
        return p


class EngineTests(Fixture):
    def test_symlink_and_real_metadata_copy(self):
        movie=self.file('Movie (2024)/Movie.mkv')
        self.file('Movie (2024)/movie.nfo',b'<movie/>')
        self.file('Movie (2024)/Movie.zh.srt',b'subtitle')
        result=self.engine.scan(self.lib)
        self.assertEqual(result['change_count'],3)
        link=self.output/'Movie (2024)/Movie.mkv'
        self.assertTrue(link.is_symlink()); self.assertEqual(link.resolve(),movie.resolve())
        self.assertEqual((self.output/'Movie (2024)/movie.nfo').read_bytes(),b'<movie/>')
        self.assertFalse((self.output/'Movie (2024)/movie.nfo').is_symlink())
        self.assertEqual(self.engine.scan(self.lib)['change_count'],0)

    def test_updated_metadata(self):
        self.file('movie.nfo',b'old'); self.engine.scan(self.lib)
        self.file('movie.nfo',b'new content'); self.engine.scan(self.lib)
        self.assertEqual((self.output/'movie.nfo').read_bytes(),b'new content')
    def test_symlink_platform_target_representation(self):
        self.file('Movie.mkv')
        original=os.readlink
        with patch('bridge.engine.os.readlink',side_effect=lambda p:'NT:'+original(p)):
            self.engine.scan(self.lib)
            self.assertTrue(self.state.rows('pt')[0]['fingerprint'].startswith('NT:'))
            self.assertEqual(self.engine.scan(self.lib)['change_count'],0)

    def test_does_not_read_video_bytes(self):
        self.file('Movie.mkv')
        with patch('bridge.engine.file_hash',side_effect=AssertionError('video content read')):
            self.engine.scan(self.lib); self.engine.scan(self.lib)

    def test_preserves_unmanaged_files(self):
        self.file('Movie.mkv'); self.output.mkdir(); (self.output/'Movie.mkv').write_bytes(b'local original')
        r=self.engine.scan(self.lib)
        self.assertEqual((self.output/'Movie.mkv').read_bytes(),b'local original')
        self.assertTrue(r['errors']); self.assertEqual(self.state.rows('pt'),[])

    def test_preserves_scraper_modified_metadata(self):
        self.file('movie.nfo',b'old'); self.engine.scan(self.lib)
        (self.output/'movie.nfo').write_bytes(b'edited by scraper')
        self.file('movie.nfo',b'updated source')
        self.engine.scan(self.lib)
        (self.source/'movie.nfo').unlink()
        self.engine.scan(self.lib,cleanup=True); self.engine.scan(self.lib,cleanup=True)
        self.assertEqual((self.output/'movie.nfo').read_bytes(),b'edited by scraper')

    def test_deletion_requires_two_observations_and_prunes_owned_dirs(self):
        self.file('Film/Movie.mkv'); self.engine.scan(self.lib)
        (self.source/'Film/Movie.mkv').unlink()
        self.engine.scan(self.lib,cleanup=True)
        self.assertTrue(os.path.lexists(self.output/'Film/Movie.mkv'))
        self.engine.scan(self.lib,cleanup=True)
        self.assertFalse((self.output/'Film').exists()); self.assertEqual(self.state.rows('pt'),[])

    def test_prune_preserves_external_metadata(self):
        self.file('Film/Movie.mkv'); self.engine.scan(self.lib)
        (self.output/'Film/poster.jpg').write_bytes(b'local poster')
        (self.source/'Film/Movie.mkv').unlink()
        self.engine.scan(self.lib,cleanup=True); self.engine.scan(self.lib,cleanup=True)
        self.assertTrue((self.output/'Film/poster.jpg').exists())

    def test_missing_marker_stops_cleanup(self):
        self.file('Movie.mkv'); self.engine.scan(self.lib)
        (self.source/'Movie.mkv').unlink(); (self.source/self.lib['sentinel']).unlink()
        with self.assertRaises(OSError): self.engine.scan(self.lib,cleanup=True)
        self.assertTrue(os.path.lexists(self.output/'Movie.mkv'))

    def test_partial_listing_never_deletes(self):
        self.file('Movie.mkv'); self.engine.scan(self.lib)
        def broken(_,errors=None,progress=None,exclude_dirs=(),workers=1):
            yield self.source/'x',None
            raise PermissionError('remote listing failed')
        with patch('bridge.engine.walk',broken),self.assertRaises(PermissionError):
            self.engine.scan(self.lib,cleanup=True)
        self.assertIsNone(self.state.rows('pt')[0]['missing_since'])
    def test_transient_cloud_directory_open_is_retried(self):
        self.file('Film/Movie.mkv')
        original=os.scandir;failed=[]
        def flaky(p):
            if Path(p)==self.source/'Film' and not failed:
                failed.append(True);raise FileNotFoundError('cloud directory is warming')
            return original(p)
        with patch('bridge.paths.os.scandir',side_effect=flaky),patch('bridge.paths.time.sleep'):
            files=list(walk(self.source))
        self.assertIn('Movie.mkv',[p.name for p,_ in files]);self.assertEqual(len(failed),1)
    def test_empty_virtual_windows_directory(self):
        empty=self.source/'Empty';empty.mkdir();original=os.scandir
        def cloud_scandir(p):
            if Path(p)==empty:
                error=FileNotFoundError('empty directory lacks dot entries');error.winerror=2;raise error
            return original(p)
        with patch('bridge.paths.os.scandir',side_effect=cloud_scandir):
            self.assertEqual([p for p,_ in walk(self.source)],[self.source/self.lib['sentinel']])
    def test_walk_uses_cached_entry_stat_without_extra_path_queries(self):
        p=self.file('Film/Movie.mkv');original=__import__('bridge.paths',fromlist=['is_link']).is_link
        checked=[]
        def check(path):checked.append(path);return original(path)
        with patch('bridge.paths.is_link',side_effect=check):files=list(walk(self.source))
        self.assertIn(p,[x for x,_ in files]);self.assertNotIn(p,checked)
    def test_partial_snapshot_updates_readable_files_without_cleanup(self):
        self.file('Good/a.mkv');self.file('Bad/b.mkv');self.engine.scan(self.lib)
        (self.source/'Good/a.mkv').unlink();self.file('Good/new.mkv')
        original=os.scandir
        def denied(p):
            if Path(p)==self.source/'Bad':raise PermissionError('blocked source subtree')
            return original(p)
        with patch('bridge.paths.os.scandir',side_effect=denied):
            result=self.engine.scan(self.lib,cleanup=True)
        self.assertFalse(result['scan_complete'])
        self.assertTrue((self.output/'Good/new.mkv').is_symlink())
        self.assertTrue(os.path.lexists(self.output/'Good/a.mkv'))
        self.assertTrue(all(r['missing_since'] is None for r in self.state.rows('pt')))

    def test_delete_circuit_breaker(self):
        self.file('Movie.mkv'); self.engine.scan(self.lib)
        (self.source/'Movie.mkv').unlink(); self.config['sync']['max_delete_fraction']=0.2
        result=self.engine.scan(self.lib,cleanup=True)
        self.assertIn('删除熔断',''.join(result['errors']))
        self.assertIsNone(self.state.rows('pt')[0]['missing_since'])

    def test_empty_snapshot_guard(self):
        self.file('Movie.mkv'); self.engine.scan(self.lib)
        (self.source/'Movie.mkv').unlink(); self.config['sync']['allow_empty_cleanup']=False
        self.assertIn('空快照保护',''.join(self.engine.scan(self.lib,cleanup=True)['errors']))

    def test_dry_run_has_no_side_effects(self):
        self.file('Movie.mkv')
        result=self.engine.scan(self.lib,dry_run=True,cleanup=True)
        self.assertEqual(result['change_count'],1); self.assertFalse(self.output.exists())
        self.assertEqual(self.state.rows('pt'),[])

    def test_missing_timestamp_reset_on_reappearance(self):
        p=self.file('Movie.mkv'); self.engine.scan(self.lib)
        old=p.stat(); p.unlink(); self.engine.scan(self.lib,cleanup=True)
        self.file('Movie.mkv'); os.utime(p,ns=(old.st_atime_ns,old.st_mtime_ns))
        self.engine.scan(self.lib)
        self.assertIsNone(self.state.rows('pt')[0]['missing_since'])

    def test_strm_unicode_and_url_encoding(self):
        self.lib.update(mode='strm',strm_prefix='http://127.0.0.1:19798/static/pt')
        self.file('电影 A/电影 #1.mkv'); self.engine.scan(self.lib)
        text=(self.output/'电影 A/电影 #1.strm').read_text()
        self.assertIn('%E7%94%B5%E5%BD%B1%20A/',text); self.assertIn('%231.mkv',text)

    def test_strm_local_absolute_path(self):
        self.lib['mode']='strm'; movie=self.file('Movie.mkv'); self.engine.scan(self.lib)
        self.assertEqual((self.output/'Movie.strm').read_text(),str(movie)+'\n')

    def test_strm_collision_stops_entire_scan(self):
        self.lib['mode']='strm'; self.file('Movie.mkv'); self.file('Movie.mp4')
        with self.assertRaisesRegex(ValueError,'冲突'):self.engine.scan(self.lib)
        self.assertFalse(self.output.exists())

    def test_source_symlink_not_traversed(self):
        outside=self.root/'outside'; outside.mkdir(); (outside/'secret.mkv').write_bytes(b'private')
        (self.source/'escape').symlink_to(outside,target_is_directory=True)
        self.engine.scan(self.lib)
        self.assertFalse((self.output/'escape').exists())
    def test_recycle_directories_excluded_from_full_and_event_scans(self):
        self.file('.deletedByTMM/Old/Movie.mkv');self.file('New/Movie.mkv')
        self.engine.scan(self.lib)
        self.assertFalse((self.output/'.deletedByTMM').exists())
        self.assertTrue((self.output/'New/Movie.mkv').is_symlink())
        result=self.engine.scan(self.lib,scope='.deletedByTMM/Old',cleanup=True)
        self.assertTrue(result['skipped']);self.assertEqual(len(self.state.rows('pt')),1)

    def test_output_junction_escape_rejected(self):
        self.file('Film/Movie.mkv'); self.output.mkdir(); outside=self.root/'outside'; outside.mkdir()
        (self.output/'Film').symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError): self.engine.scan(self.lib)
        self.assertEqual(list(outside.iterdir()),[])

    def test_metadata_size_bound(self):
        self.config['sync']['metadata_max_mb']=1
        self.file('movie.nfo',b'x'*(1024*1024+1))
        self.assertTrue(self.engine.scan(self.lib)['errors'])

    def test_scoped_scan_does_not_delete_other_folders(self):
        self.file('A/a.mkv'); self.file('B/b.mkv'); self.engine.scan(self.lib)
        self.engine.scan(self.lib,scope='A',cleanup=True)
        self.assertIsNone(next(r for r in self.state.rows('pt') if r['rel']=='B/b.mkv')['missing_since'])

    def test_directory_rename(self):
        self.file('Old/movie.mkv'); self.engine.scan(self.lib)
        (self.source/'Old').rename(self.source/'New')
        self.engine.scan(self.lib,cleanup=True); self.engine.scan(self.lib,cleanup=True)
        self.assertTrue((self.output/'New/movie.mkv').is_symlink())
        self.assertFalse((self.output/'Old').exists())


class ConfigTests(Fixture):
    def test_config_valid(self): validate(self.config,self.data)
    def test_overlapping_roots_rejected(self):
        self.lib['output']=str(self.source/'out')
        with self.assertRaises(ValueError):validate(self.config,self.data)
    def test_cross_library_overlap_rejected(self):
        other=copy.deepcopy(self.lib); other['id']='second'; other['source']=str(self.output/'bad')
        self.config['libraries'].append(other)
        with self.assertRaises(ValueError): validate(self.config,self.data)
    def test_root_output_rejected(self):
        self.lib['output']='Y:\\'
        with self.assertRaises(ValueError):validate(self.config,self.data)
    def test_restore_secrets_by_id_after_reordering(self):
        self.config['servers'][0]['token']='secret-plex'; self.config['servers'][1]['token']='secret-jellyfin'
        c=redact(self.config); c['servers'].reverse()
        new=restore_secrets(c,self.config)
        self.assertEqual(new['servers'][0]['token'],'secret-jellyfin')
        self.assertNotIn('secret-plex',json.dumps(c))
    def test_preserve_explicit_cleared_secret(self):
        self.config['cloud']['token']='before'; new=redact(self.config); new['cloud']['token']=''
        self.assertEqual(restore_secrets(new,self.config)['cloud']['token'],'')
    def test_path_traversal_rejected(self):
        for p in ('../secret','/outside','A/../../x','C:\\secret','a\\b'):
            with self.subTest(p=p),self.assertRaises(ValueError):safe_join(self.output,p)
    def test_transfer_loop_rejected(self):
        self.config['transfers']=[{'id':'a','enabled':True,'source':str(self.root/'inbox'),'destination':str(self.source),'stable_seconds':10},
                                  {'id':'b','enabled':True,'source':str(self.source),'destination':str(self.root/'inbox'),'stable_seconds':10}]
        with self.assertRaises(ValueError):validate(self.config,self.data)


class ProtocolTests(unittest.TestCase):
    def trailer(self,status=0):
        b=f'grpc-status: {status}\r\n'.encode();return b'\x80'+len(b).to_bytes(4,'big')+b
    def test_fragmented_frames(self):
        class Fragmented(BytesIO):
            def read(self,n=-1):return super().read(min(n,2))
        self.assertEqual(list(messages(Fragmented(frame(b'abc')+self.trailer()))),[b'abc'])
    def test_error_trailer(self):
        with self.assertRaises(ConnectionError):list(messages(BytesIO(self.trailer(7))))
    def test_truncated_and_compressed_frames(self):
        for value in (b'\0\0',frame(b'abcdef')[:-2],b'\1\0\0\0\0'):
            with self.subTest(value=value),self.assertRaises((ValueError,EOFError)):list(messages(BytesIO(value)))
    def test_frame_limit(self):
        with self.assertRaises(ValueError):list(messages(BytesIO(b'\0'+(32*1024*1024).to_bytes(4,'big'))))
    def test_change_and_rename_fields(self):
        change=b'\x08\x02'+text_field(3,'/115/old')+text_field(4,'/115/new')
        payload=b'\x08\x04'+varint(5<<3|2)+varint(len(change))+change
        event=decode_change(payload)
        self.assertEqual(event['type'],2);self.assertEqual(event['new_path'],'/115/new')
        self.assertIsNone(decode_change(b'\x08\x01'))
    def test_unknown_fields_skipped(self):
        self.assertEqual(dict(fields(text_field(32,'future')+b'\x08\x01'))[1],1)


class NoticeTests(Fixture):
    def enable(self):
        s=self.config['servers'][0];s.update(enabled=True,token='hidden-token',section_id='1');return s
    def test_outbox_persists_and_retries(self):
        self.enable(); self.file('Movie.mkv'); self.engine.scan(self.lib)
        self.assertEqual(len(self.state.query('SELECT * FROM notices')),1)
        with patch('bridge.notify.send',side_effect=OSError('unavailable')):deliver(self.state,self.config)
        row=self.state.query('SELECT * FROM notices')[0]
        self.assertEqual(row['attempts'],1);self.assertGreater(row['next_at'],time.time())
        self.state.execute('UPDATE notices SET next_at=0')
        with patch('bridge.notify.send') as fn:deliver(self.state,self.config);self.assertEqual(fn.call_count,1)
        self.assertEqual(self.state.query('SELECT * FROM notices'),[])
    def test_new_notice_during_delivery_is_not_lost(self):
        self.enable(); self.file('Movie.mkv'); self.engine.scan(self.lib)
        def concurrent_update(server,rows):
            with self.state.lock,self.state.db:
                self.state.notice(self.config,'pt',rows[0]['path'],'Deleted')
        with patch('bridge.notify.send',side_effect=concurrent_update):deliver(self.state,self.config)
        remaining=self.state.query('SELECT * FROM notices')
        self.assertEqual(len(remaining),1);self.assertEqual(remaining[0]['action'],'Deleted')
    def test_plex_request_uses_parent_path_and_header(self):
        server=self.enable();rows=[{'path':'Y:\\MediaLibrary\\Film A\\a.mkv','action':'Modified'}]
        captured=[]
        class Response:
            status=200
            def __enter__(self):return self
            def __exit__(self,*_):pass
        def opener(req,timeout):captured.append(req);return Response()
        send(server,rows,opener)
        self.assertIn('path=Y%3A%5CMediaLibrary%5CFilm+A',captured[0].full_url)
        self.assertNotIn('hidden-token',captured[0].full_url)
        self.assertEqual(captured[0].get_header('X-plex-token'),'hidden-token')
    def test_jellyfin_authorization_and_payload(self):
        server=self.config['servers'][1];server['token']='jf-key'
        captured=[]
        class Response:
            status=204
            def __enter__(self):return self
            def __exit__(self,*_):pass
        def opener(req,timeout):captured.append(req);return Response()
        send(server,[{'path':'Y:\\MediaLibrary\\a.mkv','action':'Created'}],opener)
        req=captured[0];self.assertEqual(req.get_header('Authorization'),'MediaBrowser Token="jf-key"')
        self.assertEqual(json.loads(req.data)['Updates'][0]['UpdateType'],'Created')
    def test_path_mapping_and_boundary(self):
        s=self.enable();s.update(path_from='Y:\\MediaLibrary',path_to='/media')
        self.assertEqual(map_path('Y:\\MediaLibrary\\电影\\A.mkv',s),'/media/电影/A.mkv')
        with self.assertRaises(ValueError):map_path('Y:\\MediaLibrary2\\A.mkv',s)


class TransferTests(Fixture):
    def test_no_overwrite_atomic_move(self):
        a=self.file('a',b'original');b=self.file('b',b'existing')
        with self.assertRaises(OSError):rename_noreplace(a,b)
        self.assertEqual(b.read_bytes(),b'existing');self.assertTrue(a.exists())
    def test_stable_same_volume_move(self):
        self.output.mkdir();p=self.file('Movie.mkv')
        t={'id':'inbox','source':str(self.source),'destination':str(self.output),'stable_seconds':10,'enabled':True}
        moves=Transfers(self.state);moves.initialize(t)
        moves.scan(t)
        moves.observed[('inbox',str(p))]=(moves.observed[('inbox',str(p))][0],time.time()-20)
        moved=moves.scan(t)
        self.assertEqual(moved,[str(self.output/'Movie.mkv')]);self.assertFalse(p.exists())
    def test_unfinished_file_prevents_folder_move(self):
        self.output.mkdir();self.file('Film/a.mkv');self.file('Film/a.part')
        t={'id':'inbox','source':str(self.source),'destination':str(self.output),'stable_seconds':10,'enabled':True}
        moves=Transfers(self.state);moves.initialize(t);moves.scan(t)
        self.assertEqual(moves.observed,{})


class ServiceTests(Fixture):
    def make(self):
        atomic_json(self.data/'config.json',self.config)
        return Service(self.data)
    def test_push_directory_rename_enqueues_old_and_new(self):
        s=self.make()
        try:
            s.cloud_paths={'pt':'/115/pt'}
            s.change({'path':'/115/pt/A/old','new_path':'/115/pt/B/new','type':2,'directory':True})
            self.assertEqual(set(s.pending),{('pt','A'),('pt','B')})
            s.change({'path':'/115/pt2/other','new_path':'','type':0,'directory':False})
            self.assertEqual(len(s.pending),2)
        finally:s.state.db.close()
    def test_debounce_and_root_collapse(self):
        s=self.make()
        try:
            s.schedule('pt','A');first=s.pending[('pt','A')];s.schedule('pt','A')
            self.assertEqual(s.pending[('pt','A')],first)
            s.schedule('pt');s.schedule('pt','B');self.assertEqual(list(s.pending),[('pt','')])
        finally:s.state.db.close()
    def test_live_config_cannot_retarget_managed_library(self):
        self.file('Movie.mkv');self.engine.scan(self.lib);s=self.make()
        try:
            c=copy.deepcopy(self.config);c['libraries'][0]['output']=str(self.root/'elsewhere')
            with self.assertRaises(ValueError):s.save_config(c)
        finally:s.state.db.close()
    def test_fresh_files_are_rescheduled_after_settling(self):
        self.config['sync']['settle_seconds']=15
        p=self.file('Movie.mkv');os.utime(p,None);s=self.make()
        try:
            result=s.perform('scan','pt')[0]
            self.assertEqual(result['deferred'],1);self.assertIn(('pt',''),s.pending)
            self.assertFalse(self.output.exists())
        finally:s.state.db.close()


class CoverTests(Fixture):
    def test_cover_size_and_output(self):
        try:from PIL import Image
        except ImportError:self.skipTest('Pillow unavailable')
        f=self.data/'cover.png';generate(self.lib,f,'私人影院')
        with Image.open(f) as img:self.assertEqual(img.size,(1600,900));self.assertEqual(img.format,'PNG')


if __name__ == '__main__':unittest.main()
