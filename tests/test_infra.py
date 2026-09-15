import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests
from follow_service import config as cfg
from follow_service import infra


def response(status=200, body=None, headers=None):
    r = requests.Response()
    r.status_code = status
    r._content_consumed = True
    r.headers.update(headers or {})
    r._content = json.dumps(body if body is not None else {}).encode()
    return r


class InfraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'config_123456.json'
        self.path.write_text(json.dumps({'hl_api_url': 'https://api.hyperliquid.xyz',
                                        'hl_info_url': 'https://read.example',
                                        'hyper_metadata_cache_dir': self.temp.name}))
        cfg.set_config_path(self.path)

    def test_read_retries_429_but_not_401(self):
        with patch.object(infra.time, 'sleep') as sleep:
            send = unittest.mock.Mock(side_effect=[response(429, headers={'Retry-After': '2'}), response(body={'ok': True})])
            self.assertEqual(infra.request_json(send, 'test'), {'ok': True})
            self.assertGreaterEqual(sleep.call_args.args[0], 2)
            send = unittest.mock.Mock(return_value=response(401))
            with self.assertRaises(infra.ReadError):
                infra.request_json(send, 'test')
            self.assertEqual(send.call_count, 1)

    def test_retry_after_exceeding_budget_is_not_retried_early(self):
        send = unittest.mock.Mock(return_value=response(429, headers={'Retry-After': '120'}))
        with patch.object(infra.time, 'sleep') as sleep:
            with self.assertRaises(infra.ReadError) as error:
                infra.request_json(send, 'test', budget=5)
            self.assertGreaterEqual(error.exception.retry_after, 120)
            sleep.assert_not_called()

    def test_metadata_shared_but_account_values_never_cached(self):
        with patch.object(infra.requests, 'post', return_value=response(body={'universe': []})) as post:
            infra.post_info('https://read.example', {'type': 'meta'})
            # Different instance, same endpoint and metadata cache.
            other = self.path.with_name('config_654321.json')
            other.write_text(self.path.read_text())
            cfg.set_config_path(other)
            infra.post_info('https://read.example', {'type': 'meta'})
            self.assertEqual(post.call_count, 1)
            post.return_value=response(body={'marginSummary':{'accountValue':'1'},'withdrawable':'1','assetPositions':[]})
            infra.post_info('https://read.example', {'type': 'clearinghouseState', 'user': '0x1'})
            infra.post_info('https://read.example', {'type': 'clearinghouseState', 'user': '0x1'})
            self.assertEqual(post.call_count, 3)
            post.return_value=response(body={'universe':[]})
            infra.post_info('https://other.example', {'type': 'meta'})
            self.assertEqual(post.call_count, 4)

    def test_errors_and_malformed_metadata_not_cached(self):
        with patch.object(infra.requests, 'post', return_value=response(body={'error': 'bad'})) as post:
            for _ in range(2):
                with self.assertRaises(ValueError):
                    infra.post_info('https://read.example', {'type': 'meta'})
            self.assertEqual(post.call_count, 2)

    def test_read_url_does_not_change_network(self):
        self.assertEqual(cfg.get_info_url(), 'https://read.example')
        self.assertEqual(cfg.get('hl_api_url'), 'https://api.hyperliquid.xyz')

    def test_metadata_single_flight_threads_and_failure_cooldown(self):
        from concurrent.futures import ThreadPoolExecutor
        with patch.object(infra.requests, 'post', return_value=response(body={'universe': []})) as post:
            with ThreadPoolExecutor(max_workers=5) as pool:
                list(pool.map(lambda _: infra.post_info('https://read.example', {'type':'meta'}), range(5)))
            self.assertEqual(post.call_count, 1)
        with patch.object(infra.requests, 'post', return_value=response(429, headers={'Retry-After':'120'})) as post:
            for _ in range(2):
                with self.assertRaises(infra.ReadError):
                    infra.post_info('https://read.example', {'type':'meta', 'dex':'xyz'})
            self.assertEqual(post.call_count, 1)

    def test_sdk_read_write_endpoints_and_no_exchange_init_reads(self):
        from follow_service import trader
        cfg.set_value('private_key', '0x' + '22' * 32)  # Public deterministic test fixture only.
        cfg.set_value('main_address', '0x' + '11' * 20)
        def send(url, *, json, timeout):
            self.assertEqual(url, 'https://read.example/info')
            if json['type'] == 'spotMeta':
                value={'universe':[], 'tokens':[]}
            elif json['type'] == 'perpDexs':
                value=[None, {'name':'xyz'}]
            else:
                coin='xyz:SNDK' if json.get('dex') else 'BTC'
                value={'universe':[{'name':coin,'szDecimals':3,'maxLeverage':10}]}
            return response(body=value)
        with patch.object(trader, '_clients_cache', None), patch.object(trader, '_spot_meta_cache', None), patch.object(trader, '_get_relevant_dexes', return_value=['','xyz']), patch.object(trader.hyper_coins,'get_perp_dexs',return_value=['','xyz']), patch.object(infra.requests, 'post', side_effect=send), patch('hyperliquid.api.API.post', side_effect=AssertionError('Unexpected official query')):
            exchange, info=trader._build_clients()
            self.assertEqual(exchange.base_url, 'https://api.hyperliquid.xyz')
            self.assertEqual(info.base_url, 'https://read.example')
            self.assertIs(exchange.info, info)
            self.assertEqual(info.name_to_asset('xyz:SNDK'),110000)

    def test_mode_read_failure_blocks_start(self):
        from follow_service import preflight
        cfg.set_value('main_address','0x'+'11'*20)
        with patch.object(preflight,'get_account_abstraction',side_effect=infra.ReadError('mode',429)):
            self.assertFalse(preflight.check_account_abstraction(False))
            with self.assertRaises(RuntimeError): preflight.check_account_abstraction(True)

    def test_registration_deduplicates_binding_and_does_not_store_signatures(self):
        from follow_service.moss_client import MossClient
        root = Path(self.temp.name)
        def make(main):
            c=MossClient('https://moss.example','agt_x',main_address=main,wallet_address='0x1')
            c._register_request=unittest.mock.Mock(return_value=response(body={'follower_id':'flw_x','status':'active','signature':'SECRET'}))
            return c
        first, second=make('0x2'),make('0x2')
        first.register_follower();second.register_follower()
        self.assertEqual(first._register_request.call_count,1)
        self.assertEqual(second._register_request.call_count,0)
        other=make('0x3');other.register_follower()
        self.assertEqual(other._register_request.call_count,1)
        for path in (root/'registration-cache').glob('*.json'):
            self.assertNotIn('SECRET',path.read_text())
            self.assertNotIn('signature',path.read_text())

    def test_moss_get_retries_but_reporting_post_never_retries(self):
        from follow_service.moss_client import MossClient
        c=MossClient('https://moss.example','agt_x')
        c._follower_sign=unittest.mock.Mock(return_value={})
        with patch.object(c._session,'request',side_effect=[response(503),response(body={})]) as send, patch.object(infra.time,'sleep'):
            c._signed_request('GET','/read')
            self.assertEqual(send.call_count,2)
            self.assertEqual(c._follower_sign.call_count,2)
        with patch.object(c._session,'request',return_value=response(429)) as send:
            with self.assertRaises(infra.ReadError): c._signed_request('POST','/write',body={})
            self.assertEqual(send.call_count,1)

    def test_supervisor_recovers_transient_without_parallel_task(self):
        from follow_service.task_health import supervise
        async def check():
            stop=asyncio.Event();calls=[]
            async def task():
                calls.append(1)
                if len(calls)==1: raise infra.ReadError('moss',429)
                stop.set()
            async def no_wait(stop,delay): pass
            with patch('follow_service.task_health.wait',side_effect=no_wait):
                await supervise('test',task,stop)
            self.assertEqual(len(calls),2)
        asyncio.run(check())

    def test_supervisor_does_not_retry_auth_failure(self):
        from follow_service.task_health import supervise
        async def check():
            stop=asyncio.Event();calls=[]
            async def task():
                calls.append(1)
                asyncio.get_running_loop().call_soon(stop.set)
                raise infra.ReadError('moss',403)
            await supervise('test',task,stop)
            self.assertEqual(len(calls),1)
        asyncio.run(check())

    def test_metadata_single_flight_across_processes(self):
        import subprocess
        import sys
        code = '''
import json, sys, time
from pathlib import Path
from unittest.mock import patch
import requests
from follow_service import config as cfg, infra
cfg.set_config_path(sys.argv[1])
def send(*args, **kwargs):
    with open(sys.argv[2], 'a') as f: f.write('fetch\\n')
    time.sleep(0.1)
    r=requests.Response();r.status_code=200;r._content=b'{"universe":[]}';r._content_consumed=True
    return r
with patch.object(infra.requests, 'post', side_effect=send):
    infra.post_info('https://process.example', {'type':'meta'})
'''
        counter=Path(self.temp.name)/'calls.txt'
        procs=[subprocess.Popen([sys.executable,'-c',code,str(self.path),str(counter)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for _ in range(3)]
        for proc in procs:
            stdout,stderr=proc.communicate(timeout=20)
            self.assertEqual(proc.returncode,0,stderr)
        self.assertEqual(counter.read_text().splitlines(),['fetch'])

    def test_account_mode_unknown_is_not_manual(self):
        from follow_service.preflight import _normalize_account_abstraction
        for mode in ({}, None, 'newUnknownMode'):
            with self.assertRaises(ValueError): _normalize_account_abstraction(mode)
        self.assertEqual(_normalize_account_abstraction('default'),'disabled')

    def test_retry_exhaustion_and_parse_error_not_retried(self):
        with patch.object(infra.time,'sleep'):
            send=unittest.mock.Mock(return_value=response(503))
            with self.assertRaises(infra.ReadError): infra.request_json(send,'test')
            self.assertEqual(send.call_count,4)
        r=response();r._content=b'not-json'
        send=unittest.mock.Mock(return_value=r)
        with self.assertRaises(ValueError): infra.request_json(send,'test')
        self.assertEqual(send.call_count,1)

    def test_poller_startup_failure_propagates_to_supervisor(self):
        from follow_service import moss_poller
        cfg.set_value('moss_source',{'enabled':True,'base_url':'https://moss.example','agent_id':'agt_x'})
        with patch.object(moss_poller,'MossClient') as cls:
            cls.return_value.has_follower_auth.return_value=True
            cls.return_value.register_follower.side_effect=infra.ReadError('moss',429)
            with self.assertRaises(infra.ReadError):
                asyncio.run(moss_poller.run_moss_poller(asyncio.Event()))

    def test_wrapped_metadata_read_error_is_recoverable(self):
        from follow_service.task_health import read_failure
        original=infra.ReadError('metadata',429)
        try:
            raise RuntimeError('required metadata unavailable') from original
        except RuntimeError as error:
            self.assertIs(read_failure(error),original)
        self.assertIsNone(read_failure(ValueError('bad configuration')))

    def test_incomplete_account_response_is_not_zero_balance(self):
        with patch.object(infra.requests,'post',return_value=response(body={})):
            with self.assertRaises(ValueError):
                infra.post_info('https://read.example',{'type':'clearinghouseState','user':'0x1'})

    def test_ws_invalid_ready_and_clean_eof_backoff(self):
        from follow_service import moss_ws
        cfg.set_value('moss_source',{'enabled':True,'base_url':'https://moss.example','agent_id':'agt_x'})
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self,*args): pass
            async def recv(self): return json.dumps(self.ready)
            def __aiter__(self): return self
            async def __anext__(self): raise StopAsyncIteration
        for ready in ({'type':'unexpected'}, {'type':'ready'}):
            async def check():
                stop=asyncio.Event();socket=Socket();socket.ready=ready;waits=[]
                async def wait(stop,seconds): waits.append(seconds);stop.set()
                with patch.object(moss_ws,'MossClient') as cls, patch.object(moss_ws,'_init_baseline_from_bootstrap'), patch.object(moss_ws.websockets,'connect',return_value=socket), patch.object(moss_ws,'wait',side_effect=wait):
                    cls.return_value.register_follower.return_value={'follower_id':'flw_x'}
                    cls.return_value.get_bootstrap.return_value={'event_sequence':1,'positions':[]}
                    cls.return_value.get_ws_path.return_value='/ws'
                    await moss_ws.run_moss_ws(stop)
                self.assertEqual(len(waits),1)
                self.assertGreaterEqual(waits[0],5)
            asyncio.run(check())
