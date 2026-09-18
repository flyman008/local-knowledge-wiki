"""Repair lifecycle through HTTP, isolated files/DB; no real model calls."""
from test_taxonomy import client, connect, check
import json

def put(title,complete,raw=b'original',**kw):
    data={'title':title,'extract_text':'第2页图表：商品库存为十二件，单位为件。' if complete else '第2页图片未识别',
          'wiki_text':'商品库存十二件。厂商未披露测试样本，不能作为客户效果保证。',
          'parse_complete':str(complete).lower(),'missing_pages':'' if complete else '2',**kw}
    r=client.post('/api/archive_full_file',data=data,files={'file':('source.txt',raw,'text/plain')})
    check(r.status_code==200)
    return client.get('/api/receipt/'+r.json()['receipt_id']).json()

a=put('repair-test',False)
old=a['review_items'][0]
b=put('repair-test',True,correction_reason='补读第2页库存图',base_version=1)
payload={'id':old['id'],'expected_revision':1,'receipt_id':b['id'],'checked_by':'test-client',
         'coverage':[{'page':2,'locator':'第2页库存图','quote':'商品库存为十二件，单位为件。'}]}
def call(data):return client.post('/api/reviews/repair',json=data)
check(call({**payload,'coverage':[]}).status_code==400)
check(call({**payload,'coverage':[{'page':2,'locator':'p2','quote':'不在解析稿里面的虚构补读证据啊'}]}).status_code==400)
check(call({**payload,'expected_revision':9}).status_code==400)
r=call(payload);check(r.status_code==200);check(len(r.json()['closed_ids'])==2);check(r.json()['remaining_pending']==0)
check(call(payload).status_code==400) # retry cannot silently reuse stale revision
read=client.get('/api/receipt/'+b['id']).json()
check(all(x['status']=='resolved' for x in read['review_items']))
check(client.post('/api/reviews/reopen-repair',json={'id':old['id'],'expected_revision':2,'reason':'补读仍有误'}).status_code==200)
check(client.get('/api/receipt/'+b['id']).json()['review_items'][0]['status']=='pending')

for title,changed,business in [('changed-raw',True,False),('business-preserved',False,True)]:
    a=put(title,False);item=a['review_items'][0]
    if business:
        q=client.post('/api/reviews/raise',json={'kb_id':'library','doc_id':title,'topic_version':1,'kind':'conflict','question':'价格依据冲突','evidence':[{'statement':'A和B不同','locator':'p1'}]})
        check(q.status_code==200)
    b=put(title,True,raw=b'changed' if changed else b'original',correction_reason='补读第2页',base_version=1)
    r=call({**payload,'id':item['id'],'receipt_id':b['id']})
    check(r.status_code==(400 if changed else 200))
    if business:
        check(len(r.json()['closed_ids'])==1 and r.json()['remaining_pending']==2)
        check(call({**payload,'id':q.json()['id'],'receipt_id':b['id']}).status_code==400)

notice={'kb_id':'library','doc_id':'repair-test','topic_version':2,'category':'source_not_disclosed',
        'limitation':'厂商未披露测试样本，不能作为客户效果保证。','locator':'原文评测段'}
r=client.post('/api/reviews/limitation',json=notice);check(r.status_code==200 and r.json()['status']=='notice')
check(client.post('/api/reviews/limitation',json=notice).json()['is_duplicate'])
check(client.post('/api/reviews/limitation',json={**notice,'limitation':'未保存的限制'}).status_code==400)
a=put('flag-only',False,extract_text='第2页图表：商品库存为十二件，单位为件。')
b=put('flag-only',True,correction_reason='声称补齐',base_version=1)
check(call({**payload,'id':a['review_items'][0]['id'],'receipt_id':b['id']}).status_code==400)
c=connect();check(c.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0)
check(c.execute('SELECT count(*) FROM review_events').fetchone()[0]>=5);c.close()
print('PASS repair lifecycle: evidence, identity, versions, audit, reopen, business preservation, notices, zero models')
