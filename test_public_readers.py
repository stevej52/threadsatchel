"""Public reader authentication and a real stdio save/read round trip on disposable archives."""
import asyncio
from contextlib import closing
import hmac
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.server.mcpserver.exceptions import ToolError

import readonly_daemon
import reader_transport
import server_readonly as reader


ROOT = Path(__file__).resolve().parent


class PublicReaders(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/'archive'
        self.root.mkdir()
        self.db = self.root/'memory.sqlite3'
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute('CREATE TABLE memories(id TEXT PRIMARY KEY,text TEXT,source TEXT,title TEXT,created_at TEXT)')
            db.execute('CREATE VIRTUAL TABLE memory_fts USING fts5(id UNINDEXED,text,source,title)')
            db.execute("INSERT INTO memories VALUES('fixture','Synthetic reader result.','fixture','Title','2000-01-01')")
            db.execute("INSERT INTO memory_fts VALUES('fixture','Synthetic reader result.','fixture','Title')")
        self.handoffs = self.root/'handoffs'
        self.handoffs.mkdir()
        self.enterContext(patch.object(reader, 'DB', self.db))
        self.enterContext(patch.object(reader, 'HANDOFFS', self.handoffs))
        self.enterContext(patch.dict(os.environ, {'THREADSATCHEL_SHARED_READER': '0'}))

    def test_loopback_health_and_rpc_proofs_are_archive_specific(self):
        key = reader_transport.reader_key(self.root)
        other = self.root.parent/'other-archive'
        other.mkdir()
        other_key = reader_transport.reader_key(other)
        self.assertNotEqual(key, other_key)
        self.assertEqual(key, reader_transport.reader_key(self.root))
        with ThreadingHTTPServer(('127.0.0.1',0), readonly_daemon.DaemonHandler) as server:
            server.reader_key = key
            worker = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.05})
            worker.start()
            def request(method, path, body=None, headers=None):
                with closing(HTTPConnection(*server.server_address, timeout=5)) as client:
                    client.request(method, path, body, headers or {})
                    response = client.getresponse()
                    return response.status, json.loads(response.read())
            try:
                nonce = '01'*20
                self.assertEqual(request('GET','/health')[0], 403)
                status, health = request('GET','/health', headers={'X-Challenge':nonce})
                self.assertEqual(status, 200)
                self.assertTrue(reader_transport.describe_health(key,nonce,health))
                self.assertFalse(reader_transport.describe_health(other_key,nonce,health))
                self.assertFalse(reader_transport.describe_health(key,'02'*20,health))
                body = b'{"tool":"get_memory","args":{"id":"fixture"}}'
                self.assertEqual(request('POST','/rpc',body)[0],403)
                for bad_proof in (health['proof'], reader_transport.proof(other_key,b'rpc',body),
                                  reader_transport.proof(key,b'rpc',body+b' ')):
                    self.assertEqual(request('POST','/rpc',body,{'X-Request-Proof':bad_proof})[0],403)
                signed = hmac.digest(key, b'rpc\0'+body, 'sha256').hex()
                status, payload = request('POST','/rpc',body,{'X-Request-Proof':signed})
                self.assertEqual(status, 200)
                self.assertTrue(payload['ok']); self.assertEqual(payload['result']['text'],'Synthetic reader result.')
            finally:
                server.shutdown(); worker.join(timeout=5)

    def test_reject_arbitrary_paths_and_linked_ancestors(self):
        outside = self.root.parent/'outside'; outside.mkdir()
        (outside/'private.txt').write_text('Synthetic external text.', encoding='utf-8')
        for name in ('../outside/private.txt',r'..\outside\private.txt',str(outside/'private.txt'),
                     'Example/v1/../../../outside/private.txt','Example/v1/private.txt:stream'):
            with self.subTest(name=name), self.assertRaises(ToolError):
                reader.read_handoff_file(name)
        linked = self.handoffs/'redirect'
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                raise
            # Directory junctions work for unprivileged Windows test accounts.
            import _winapi
            _winapi.CreateJunction(str(outside), str(linked))
        self.assertFalse(any(h.get('name') == 'redirect' for h in reader.list_handoffs()['handoffs']))
        with self.assertRaises(ToolError):
            reader.read_handoff_file('redirect/private.txt')

    def test_stdio_save_and_read_exact_version_in_pages(self):
        # Both real stdio entry points use their own installation directory.
        # Copy source only. Starting either one cannot open an existing archive.
        for source in ROOT.glob('*.py'):
            if not source.name.startswith('test_'):
                shutil.copyfile(source, self.root/source.name)
        connector = self.root/'connector'; connector.mkdir()
        for name in ('__init__.py','connector.py'):
            shutil.copyfile(ROOT/'connector'/name, connector/name)

        def connect(script):
            return Client(StdioServerParameters(command=sys.executable,
                args=['-B',str(self.root/script)], env={'THREADSATCHEL_SHARED_READER':'0'}))

        async def read(client, tool, args):
            reply = await client.call_tool(tool, args)
            self.assertFalse(reply.is_error, reply.content)
            value = json.loads(next(c.text for c in reply.content if c.type=='text'))
            return value['result'] if set(value) == {'result'} else value

        async def check():
            old_text = 'A🛞 café\nSecond line.'
            async with connect('server_save.py') as client:
                note = await read(client,'save_memory',{'title':'Sample','text':'Synthetic note to read later.'})
                self.assertTrue(note['ok']); self.assertTrue(note['result']['saved'])
                args = dict(name='Example',instructions='First revision',files=[dict(name='report.txt', text=old_text)])
                first = await read(client,'save_handoff',args)
                self.assertTrue(first['ok']); self.assertEqual(first['result']['version'],'v1')
                repeated = await read(client,'save_handoff',args)
                self.assertEqual(repeated['result']['version'],'v1')
                args.update(instructions='Second revision',files=[dict(name='report.txt',text='Second version only.')])
                second = await read(client,'save_handoff',args)
                self.assertTrue(second['ok']); self.assertEqual(second['result']['version'],'v2')
            (self.handoffs/'Example.txt').write_text('Unversioned legacy sibling.',encoding='utf-8')
            async with connect('server_readonly.py') as client:
                old = await read(client,'get_handoff',dict(name='Example',version='v1'))
                self.assertEqual(old['version'],'v1'); self.assertEqual(old['versions'],['v1','v2'])
                self.assertTrue(all(f['path'].startswith('Example/v1/') for f in old['files']))
                path = next(f['path'] for f in old['files'] if f['path'].endswith('/report.txt'))
                first = await read(client,'read_handoff_file',dict(path=path,offset=0,length=4))
                self.assertEqual(first['text'],old_text[:4]); self.assertFalse(first['eof'])
                rest = await read(client,'read_handoff_file',dict(path=path,offset=first['next_offset']))
                self.assertTrue(rest['eof']); self.assertEqual(first['text']+rest['text'],old_text)
                fresh = await read(client,'get_handoff',dict(name='Example',version='v2'))
                second_path = next(f['path'] for f in fresh['files'] if f['path'].endswith('/report.txt'))
                self.assertEqual((await read(client,'read_handoff_file',dict(path=second_path)))['text'],'Second version only.')
                missing = await client.call_tool('get_handoff',dict(name='Example',version='v99'))
                self.assertTrue(missing.is_error)
            with closing(sqlite3.connect(self.db)) as db:
                self.assertEqual(db.execute("SELECT count(*) FROM memories WHERE text='Synthetic note to read later.'").fetchone()[0],1)
        asyncio.run(check())
