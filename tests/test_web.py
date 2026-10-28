from io import BytesIO
import json
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from bridge.web import Handler
from bridge.cloud import CloudClient
from bridge.protocol import frame, text_field, varint


class FakeSocket:
    def __init__(self, request):self.input=BytesIO(request);self.output=BytesIO()
    def makefile(self,*_):return self.input
    def settimeout(self,*_):pass
    def sendall(self,data):self.output.write(data)


class WebTests(unittest.TestCase):
    def setUp(self):
        self.service=Mock()
        self.service.config={'cloud':{'token':'private-token'}}
        self.service.status.return_value={'busy':False,'managed':[]}
        self.server=SimpleNamespace(auth_lock=threading.Lock(),sessions={},attempts={},admin_key='a'*40,service=self.service)

    def request(self,path,body=None,cookie=None,origin=None,custom=True):
        payload=json.dumps(body,ensure_ascii=False).encode() if body is not None else b''
        method='POST' if body is not None else 'GET'
        headers=[f'{method} {path} HTTP/1.0','Host: 127.0.0.1:8095']
        if body is not None:headers+=['Content-Type: application/json',f'Content-Length: {len(payload)}']
        if custom:headers+=['X-Media-Bridge: 1']
        if cookie:headers+=['Cookie: '+cookie]
        if origin:headers+=['Origin: '+origin]
        sock=FakeSocket(('\r\n'.join(headers)+'\r\n\r\n').encode()+payload)
        Handler(sock,('127.0.0.1',1234),self.server)
        head,data=sock.output.getvalue().split(b'\r\n\r\n',1)
        return head.decode(),data

    def login(self):
        head,data=self.request('/api/login',{'key':'a'*40})
        self.assertIn('200 OK',head)
        cookie=next(x.split(': ',1)[1].split(';',1)[0] for x in head.split('\r\n') if x.startswith('Set-Cookie:'))
        return cookie

    def test_login_cookie_and_authenticated_state(self):
        cookie=self.login();head,data=self.request('/api/status',cookie=cookie)
        self.assertIn('200 OK',head);self.assertEqual(json.loads(data)['busy'],False)

    def test_unauthenticated_api_is_denied(self):
        head,_=self.request('/api/config');self.assertIn('401 Unauthorized',head)

    def test_configuration_is_redacted(self):
        head,data=self.request('/api/config',cookie=self.login())
        self.assertNotIn(b'private-token',data);self.assertIn(b'********',data)

    def test_configuration_save_returns_job_without_waiting_for_apply(self):
        self.service.submit_config.return_value = {'id':'config-job', 'action':'config', 'status':'排队中'}
        head, data = self.request('/api/config', {'cloud':{'token':'********'}}, cookie=self.login())
        self.assertIn('202 Accepted', head)
        self.assertEqual(json.loads(data)['id'], 'config-job')
        self.service.submit_config.assert_called_once_with({'cloud':{'token':'private-token'}})
        self.service.save_config.assert_not_called()
        self.assertNotIn(b'private-token', data)

    def test_cross_origin_write_is_denied(self):
        head,_=self.request('/api/login',{'key':'a'*40},origin='https://other.invalid')
        self.assertIn('403 Forbidden',head)
        head,_=self.request('/api/login',{'key':'a'*40},custom=False)
        self.assertIn('403 Forbidden',head)

    def test_initialization_requires_confirmation(self):
        cookie=self.login()
        head,_=self.request('/api/actions',{'action':'initialize','library':'pt'},cookie=cookie)
        self.assertIn('400 Bad Request',head);self.service.submit.assert_not_called()

    def test_login_rate_limit(self):
        for _ in range(10):self.request('/api/login',{'key':'wrong'})
        head,_=self.request('/api/login',{'key':'a'*40})
        self.assertIn('429 Too Many Requests',head)

    def test_expired_cookie_is_denied(self):
        self.server.sessions['expired']=time.time()-1
        head,_=self.request('/api/status',cookie='mb_session=expired')
        self.assertIn('401 Unauthorized',head)

    def test_logout_invalidates_session(self):
        cookie=self.login();self.request('/api/logout',{},cookie=cookie)
        head,_=self.request('/api/status',cookie=cookie)
        self.assertIn('401 Unauthorized',head)

    def test_static_page_and_security_headers(self):
        head,data=self.request('/')
        self.assertIn('200 OK',head);self.assertIn('Content-Security-Policy:',head)
        self.assertIn(b'login-form',data)
        self.assertNotIn(b'192.168.', data)
        self.assertNotIn(b'Windows Server', data)
        self.assertIn(b'AutoSync Symlink', data)


class CloudTests(unittest.TestCase):
    def trailer(self):
        b=b'grpc-status: 0\r\n';return b'\x80'+len(b).to_bytes(4,'big')+b

    def test_subscribe_decodes_real_framed_event(self):
        c=CloudClient({'url':'http://127.0.0.1:19798','token':'token','username':'','password':''})
        nested=text_field(3,'/115/pt/A.mkv')
        msg=b'\x08\x04'+varint(5<<3|2)+varint(len(nested))+nested
        response=BytesIO(frame(msg)+self.trailer());conn=Mock();events=[];connected=Mock()
        c.open=Mock(return_value=(conn,response))
        c.subscribe(events.append,connected,threading.Event())
        self.assertEqual(events[0]['path'],'/115/pt/A.mkv');connected.assert_called_once()
        c.open.assert_called_once_with('PushMessage',token='token',timeout=75)
        conn.sock.settimeout.assert_called_once_with(None);conn.close.assert_called_once()

    def test_mount_mapping_schema(self):
        c=CloudClient({'token':'token'})
        mount=text_field(1,'M:\\')+text_field(2,'/115')+b'\x48\x01'
        msg=varint(1<<3|2)+varint(len(mount))+mount
        conn=Mock();c.open=Mock(return_value=(conn,BytesIO(frame(msg)+self.trailer())))
        self.assertEqual(c.mount_points(),[{'mount':'M:\\','source':'/115','mounted':True}])

    def test_password_login_schema(self):
        c=CloudClient({'token':'','username':'user','password':'password'})
        msg=b'\x08\x01'+text_field(3,'returned-jwt')
        conn=Mock();c.open=Mock(return_value=(conn,BytesIO(frame(msg)+self.trailer())))
        self.assertEqual(c.token(),'returned-jwt')
        c.open.assert_called_once_with('GetToken',text_field(1,'user')+text_field(2,'password'))


if __name__=='__main__':unittest.main()
