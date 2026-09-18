"""Single-store multi-scope/tag acceptance. Isolated filesystem/DB; no real LLM."""
import sys,sqlite3,tempfile,json
from pathlib import Path
sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import config,db,domains,archive_full,archive,ingest,search
root=Path(tempfile.mkdtemp(prefix='knowledge-taxonomy-'))
config.LOCAL_DIR=root/'local'
cfg=config.Config(config.LLMConfig('http://invalid','fake','fake','NO_KEY',10,1),config.ServiceConfig('127.0.0.1',8765,1,1,10),[config.KnowledgeBase('library',str(root/'content'),'统一知识库','private',False,None)],str(root/'content'))
def connect(*args,**kwargs):
    c=sqlite3.connect(root/'test.db');c.row_factory=sqlite3.Row
    c.executescript(db.SCHEMA);db._migrate(c);return c
db.get_conn=connect
config.load_config=lambda:cfg
import app
from fastapi.testclient import TestClient
client=TestClient(app.app)
count=0
def check(value):
    global count
    assert value
    count+=1
def prepared(title,scopes,tags):
    r=client.post('/api/archive_full_file',data={'title':title,'extract_text':'extract','wiki_text':'sharedtoken 共同主题 '+title,'classification':json.dumps({'scopes':scopes,'tags':tags})},files={'file':(title+'.txt',title.encode(),'text/plain')})
    check(r.status_code==200)
    return r.json()
a=prepared('weimob-ai',['weimob'],['AI','SaaS'])
b=prepared('external-ai',['external'],['AI'])
c=prepared('comparison',['weimob','external'],['AI','SaaS'])
d=prepared('unknown',[],[])
for query in ('sharedtoken','共同'):
    check(len(search.search(cfg,query))==4)
    check(len(search.search(cfg,query,scope='weimob'))==2)
    check(len(search.search(cfg,query,scope='external',tag='SaaS'))==1)
    check(len(search.search(cfg,query,tag='ai'))==3)
check(prepared('comparison',['external','weimob'],['SaaS','AI'])['is_duplicate'])
conn=connect();check(conn.execute('SELECT count(*) FROM receipts').fetchone()[0]==4);conn.close()
check(ingest.get_receipt(cfg,c['receipt_id'])['classification']['scopes']==['external','weimob'])
check(client.post('/api/archive_full',json={'kb_id':'saas','title':'old'}).status_code==422)
check(client.post('/api/archive_full',json={'title':'bad','classification':{'scopes':['foo']}}).status_code==400)
check(client.get('/api/search?q=sharedtoken&scope=external&tag=SaaS').json()[0]['doc_id']==c['doc_id'])
check(client.get('/api/capabilities').json()['taxonomy']=='scopes-tags-v1')
check(len(client.get('/api/knowledge_bases').json())==1)
check(all('classification' in t for t in client.get('/api/topics').json()))
arch=client.post('/api/archive',json={'title':'arch','text':'archtoken','classification':{'scopes':['weimob'],'tags':['SaaS']}})
check(arch.status_code==200 and len(search.search(cfg,'archtoken',scope='weimob',tag='SaaS'))==1)
for endpoint in ('/api/archive_file','/api/submit_file'):
    r=client.post(endpoint,data={'classification':json.dumps({'scopes':['external'],'tags':['AI']})},files={'file':('fixture.txt',b'filetoken','text/plain')})
    check(r.status_code==200)
    check(ingest.get_receipt(cfg,r.json()['receipt_id'])['classification']['tags']==['AI'])
sub=client.post('/api/submit',json={'text':'compiletoken','classification':{'scopes':['weimob'],'tags':['AI']}})
check(sub.status_code==200)
check(ingest.get_receipt(cfg,sub.json()['receipt_id'])['classification']['scopes']==['weimob'])
conn=connect();check(conn.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0);conn.close()
print(f'PASS {count} taxonomy checks; isolated data at {root}; zero model calls')
