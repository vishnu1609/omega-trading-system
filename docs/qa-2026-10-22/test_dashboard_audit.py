"""Isolated API audit. Run from repository root: python3 -m pytest docs/qa-2026-10-22/test_dashboard_audit.py -q.
Broker, DB and wallet are fakes; every filesystem write uses pytest tmp_path.
Failures express expected safe behavior, not approved current behavior.
"""
import csv, io, json
from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from webhook import dashboard as d
from core.position_manager import Position, PositionManager
from core.broker_base import OrderResponse
from core.database import OmegaDB

@pytest.fixture
def env(monkeypatch,tmp_path):
 monkeypatch.chdir(tmp_path)
 (tmp_path/'data').mkdir()
 monkeypatch.setattr(d,'STATE_FILE',tmp_path/'data/state.json')
 pm=PositionManager(); broker=MagicMock(); wallet=MagicMock(); wallet.balance=10000
 broker.get_ltp.return_value=110
 broker.place_order.return_value=OrderResponse(order_id='TEST',status='TRADED',fill_price=112,fill_quantity=10)
 broker.modify_order.return_value=OrderResponse(order_id='SL',status='MODIFIED')
 db=MagicMock();monkeypatch.setattr(OmegaDB,'get',lambda:db)
 monkeypatch.setattr(d,'_bot',dict(pm=pm,broker=broker,wallet=wallet))
 pos=Position(symbol='NIFTY24500CE',security_id='101',exchange_segment='NSE_FNO',option_type='CE',direction='LONG',quantity=10,entry_price=100,current_price=110,sl_price=80,tp_price=140,sl_order_id='SL')
 pm.add_position(pos)
 app=FastAPI();app.include_router(d.router)
 client=TestClient(app,raise_server_exceptions=False)
 client.headers['X-CSRF-Token']=d._CSRF_TOKEN
 return client,pm,broker,wallet,pos

@pytest.mark.parametrize('route',['/','/dashboard','/health','/api/state','/api/export_csv','/api/trade_analytics'])
def test_read_routes(env,route):
 assert env[0].get(route).status_code==200

@pytest.mark.parametrize('route',['close_position','close_all','modify_sl','modify_tp','add_note'])
def test_csrf_required(env,route):
 c,*_=env
 assert c.post('/api/'+route,json={},headers={'X-CSRF-Token':'wrong'}).status_code==403

@pytest.mark.parametrize('field,value',[('sl',85),('tp',150)])
def test_modify_happy_path(env,field,value):
 c,pm,b,w,p=env
 r=c.post('/api/modify_'+field,json={'security_id':'101','new_'+field:value})
 assert r.json()['ok'];assert getattr(p,field+'_price')==value

@pytest.mark.parametrize('field,value',[('sl',0),('tp',-1),('sl','bad'),('tp',None),('sl','NaN'),('tp','Infinity')])
def test_invalid_price_is_rejected_without_mutation(env,field,value):
 c,pm,b,w,p=env;old=getattr(p,field+'_price')
 r=c.post('/api/modify_'+field,json={'security_id':'101','new_'+field:value})
 assert r.status_code<500, r.text
 assert not r.json().get('ok',False), r.text
 assert getattr(p,field+'_price')==old

def test_sl_broker_failure_preserves_stop(env):
 c,pm,b,w,p=env;b.modify_order.side_effect=RuntimeError('broker offline')
 r=c.post('/api/modify_sl',json={'security_id':'101','new_sl':90})
 assert not r.json().get('ok'),r.text
 assert p.sl_price==80

def test_single_close_fill(env):
 c,pm,b,w,p=env
 r=c.post('/api/close_position',json={'security_id':'101'})
 assert r.json()['fill_price']==112;assert pm.open_count==0;assert pm.closed_trades[0].pnl==120

@pytest.mark.parametrize('route',['close_position','close_all'])
@pytest.mark.parametrize('status,qty',[('REJECTED',0),('PENDING',0),('PART_TRADED',4)])
def test_unfilled_or_partial_order_preserves_remaining_position(env,route,status,qty):
 c,pm,b,w,p=env;b.place_order.return_value=OrderResponse(order_id='TEST',status=status,fill_price=112 if qty else 0,fill_quantity=qty)
 r=c.post('/api/'+route,json={'security_id':'101'})
 assert pm.open_count==1, f'{status}/{qty}: {r.text}; local position incorrectly removed'
 assert pm.open_positions[0].quantity==10-qty

def test_close_all_uses_actual_fill(env):
 c,pm,b,w,p=env
 r=c.post('/api/close_all',json={})
 assert r.json()['ok'];assert pm.closed_trades[0].exit_price==112

@pytest.mark.parametrize('route',['close_position','close_all'])
def test_spread_closes_hedge_leg(env,route):
 c,pm,b,w,p=env;p.is_spread=True;p.hedge_security_id='202';p.hedge_current_price=50;p.hedge_entry_price=40
 c.post('/api/'+route,json={'security_id':'101'})
 ids=[call.args[0].security_id for call in b.place_order.call_args_list]
 assert '202' in ids, f'Only submitted legs: {ids}'

def test_note_roundtrip(env):
 c,*_=env
 assert c.post('/api/add_note',json={'trade_key':'trade1','note':'review'}).json()['ok']
 assert c.get('/api/state').json()['trade_notes']['trade1']=='review'

def test_note_can_be_cleared(env):
 c,*_=env
 c.post('/api/add_note',json={'trade_key':'trade1','note':'review'})
 r=c.post('/api/add_note',json={'trade_key':'trade1','note':''})
 assert r.json()['ok'];assert not c.get('/api/state').json()['trade_notes'].get('trade1')

def test_csv_escaping(env):
 c,*_=env
 d._write_state({'indices':{'closed_trades':[{'symbol':'NIFTY,TEST','reason':'a\nb','pnl':10}]}})
 rows=list(csv.reader(io.StringIO(c.get('/api/export_csv').text)))
 assert len(rows)==2 and len(rows[1])==11,rows

def test_option_chain_fixture(env):
 c,pm,b,w,p=env;oc=MagicMock();d._bot['_oc_resolver']=oc
 oc.get_nearest_expiry.return_value='2026-10-29';oc.get_expiry_list.return_value=['2026-10-29']
 oc.fetch_chain.return_value={'data':{'last_price':24500,'oc':{'24500':{'ce':{'oi':100,'last_price':110}}}}}
 for name in ['NIFTY','BANKNIFTY','SENSEX']:
  r=c.get('/api/option_chain/'+name);assert r.status_code==200;assert r.json()['rows'][0]['ce_ltp']==110

def test_analytics_totals(env):
 c,*_=env
 d._write_state({'indices':{'closed_trades':[{'symbol':'NIFTY24500CE','pnl':100},{'symbol':'NIFTY24500CE','pnl':-50}]}})
 assert c.get('/api/trade_analytics').json()['pnl_series']==[100,50]

def test_every_tab_has_render_dispatch():
 import re
 tabs=set(re.findall(r'data-t="([a-z]+)"',d._HTML))
 dispatch=d._HTML.split('function renderTab(){',1)[1].split('/*',1)[0]
 cases=set(re.findall(r"case'([a-z]+)'",dispatch))
 assert tabs<=cases, f'Tabs without render dispatch: {tabs-cases}'

def test_reverse_engine_empty_database(env,tmp_path,monkeypatch):
 c,*_=env
 from core import reverse_engine as re
 db=OmegaDB(tmp_path/'data/omega.db')
 monkeypatch.setattr(re,'DB_PATH',str(tmp_path/'data/omega.db'))
 r=c.get('/api/reverse_engine')
 assert r.status_code==200,r.text
 assert 'autopsy' in r.json()

@pytest.fixture
def webhook_client(monkeypatch):
 import queue
 from collections import defaultdict
 from webhook import tv_receiver as tv
 monkeypatch.setenv('TV_WEBHOOK_SECRET','test-secret')
 monkeypatch.setattr(tv,'signal_queue',queue.Queue())
 monkeypatch.setattr(tv,'_request_counts',defaultdict(list))
 return TestClient(tv.app,raise_server_exceptions=False),tv

def signed_post(c,body):
 import hmac,hashlib
 raw=json.dumps(body)
 sig=hmac.new(b'test-secret',raw.encode(),hashlib.sha256).hexdigest()
 return c.post('/tv-alert',content=raw,headers={'X-Signature':sig,'Content-Type':'application/json'})

def test_webhook_happy_path(webhook_client):
 c,tv=webhook_client
 r=signed_post(c,dict(strategy='omega_complete',symbol='NIFTY',side='BUY_CE',sl=80,strike=24500))
 assert r.status_code==200;assert tv.signal_queue.get_nowait()['side']=='BUY_CE'

def test_webhook_invalid_signature(webhook_client):
 c,tv=webhook_client
 assert c.post('/tv-alert',json={}).status_code==401
 assert tv.signal_queue.empty()

@pytest.mark.parametrize('body',[{},[],{'strategy':'bad','symbol':'NIFTY','side':'BUY'}, {'strategy':'omega_complete','symbol':'NIFTY','side':'BAD'}, {'strategy':'omega_complete','symbol':'NIFTY','side':'BUY','strike':'bad'}, {'strategy':'omega_complete','symbol':'NIFTY','side':'BUY','sl':None}])
def test_webhook_bad_payload_is_client_error(webhook_client,body):
 c,tv=webhook_client;r=signed_post(c,body)
 assert 400<=r.status_code<500,r.text
 assert tv.signal_queue.empty()

def test_webhook_queue_capacity(webhook_client):
 c,tv=webhook_client
 for _ in range(tv.MAX_QUEUE_SIZE):tv.signal_queue.put({})
 assert signed_post(c,dict(strategy='omega_complete',symbol='NIFTY',side='BUY')).status_code==503
