"""Isolated confirmation lifecycle tests. No production data or real LLM."""
import sys, runpy, json
from pathlib import Path
sys.dont_write_bytecode=True
n=runpy.run_path(str(Path(__file__).with_name('test_taxonomy.py')))
import review, archive_full, search, config, db, ingest
cfg=n['cfg']
config.load_config=lambda:cfg
import app
from fastapi.testclient import TestClient
client=TestClient(app.app)
checks=0
def check(condition):
    global checks
    assert condition
    checks+=1
def post(op,payload,code=200):
    r=client.post('/api/reviews/'+op,json=payload)
    check(r.status_code==code)
    return r.json()
r=archive_full.archive_full(cfg,kb_id='library',title='reviewtoken',raw_data=b'old',raw_filename='old.txt',extract_text='extract',wiki_text='reviewtoken old',parse_complete=False,missing_pages=[2])
items=ingest.get_receipt(cfg,r.receipt_id)['review_items']
check(len(items)==1 and items[0]['status']=='pending')
raised=post('raise',{'kb_id':'library','doc_id':r.doc_id,'topic_version':1,'kind':'conflict','question':'Which price applies?',
    'evidence':[{'statement':'price=5','locator':'old.txt p1'},{'statement':'price=10','locator':'new.txt p1'}]})
decision={'id':raised['id'],'expected_revision':1,'action':'adopt','reason':'approved scope','scope':'test-only','confirmed_by':'test-user','conclusion':'price=10','basis':'new.txt p1'}
post('decide',decision,400)
decision['user_confirmed']=True
post('decide',decision)
post('decide',decision,400)
check(search.search(cfg,'reviewtoken','library')[0]['review_status']=='pending')
policy=post('propose-policy',{'id':raised['id'],'rule':'Use current price policy','scope':'test-only'})
check(not search.search(cfg,'reviewtoken','library')[0]['active_policies'])
approval={'id':policy['id'],'expected_revision':1,'action':'active','user_confirmed':True,'confirmed_by':'test-user','reason':'separate approval'}
post('policy-state',approval)
check(len(search.search(cfg,'reviewtoken','library')[0]['active_policies'])==1)
approval.update(expected_revision=2,action='revoked')
post('policy-state',approval)
check(not search.search(cfg,'reviewtoken','library')[0]['active_policies'])
for action in ('coexist','supersede','unresolved','reopen'):
    decision.update(expected_revision=decision['expected_revision']+1,action=action,effective_at='2026-09-15')
    post('decide',decision)
before=(n['root']/'inbox').exists()
r2=archive_full.archive_full(cfg,kb_id='library',title='reviewtoken',raw_data=b'new',raw_filename='new.txt',extract_text='extract2',wiki_text='reviewtoken new')
check(r2.version==2)
post('decide',dict(decision,expected_revision=6),400)
hits=search.search(cfg,'reviewtoken','library')
check(any(not i['applies_to_current_version'] for i in hits[0]['review_items']))
check(any(i['applies_to_current_version'] and i['status']=='pending' for i in hits[0]['review_items']))
class Fake:
    def __init__(self,cfg):pass
    def complete(self,messages,system=None,**kw):
        check('review_items' in messages[0]['content'] and 'applies_to_current_version' in system)
        class R:
            text='test'; model='fake'; tokens_in=0; tokens_out=0; elapsed_ms=0
        return R()
search.llm_mod.LLM=Fake
check(search.answer(cfg,'reviewtoken','library')['answer'].startswith('【待核实提醒】'))
state=client.get('/api/reviews?kb_id=library').json()
check(len(state['events'])==7)
check(client.post('/api/reviews/raise',json={'kb_id':'library','doc_id':r.doc_id,'topic_version':1,'kind':'uncertain','question':'stale','evidence':[]}).status_code==400)
import compile as compiler
conn=db.get_conn()
compiler._record_change(conn,'library',r.doc_id,True,None,'conflict','backend disagreement',['two conflicting statements'])
conn.commit()
check(any(i['question']=='backend disagreement' for i in review.context(conn,'library',r.doc_id)))
conn.close()
check(search.search(cfg,'reviewtoken','library')[0]['topic_version']==2)
print(f'PASS {checks} confirmation checks: record, consent, concurrent/stale revision, 5 actions, separate policy approval/revoke, search warning; fake model only')
