"""Isolated end-to-end checks; no live data or model calls."""
from test_taxonomy import client, connect, check
import quality

def submit(title, **kwargs):
    data = dict(title=title, raw_data=None, extract_text='第1页文字', wiki_text='内容标签：`AI`',
                source_metadata={'source_kind':'sales_material','locator':'第1页','document_date':'2026-08','date_evidence':'document_date：第1页，2026年8月'},
                classification={'scopes':['weimob'],'tags':['AI']})
    data.update(kwargs)
    data.pop('raw_data')
    r = client.post('/api/archive_full', json=data)
    check(r.status_code == 200)
    rid = r.json()['receipt_id']
    return rid, client.get('/api/receipt/'+rid).json()

rid, good = submit('quality-good')
check(good['quality']['automatic_check']=='passed')
check(good['quality']['content_review']=='not_verified')
check(good['source_metadata']['document_date']=='2026-08')
check('product_version' not in good['source_metadata'])
rid2, again = submit('quality-good')
check(rid2==rid and again['quality']==good['quality'])
_, bad = submit('quality-bad',wiki_text='内容标签：`AI`、`SaaS`\n登记为待核实',
                missing_pages=[2],parse_complete=True,extract_text='第2页：未识别')
codes={x['code'] for x in bad['quality']['findings']}
check({'tag_mismatch','registration_claim','parse_contradiction','parse_text_warning'} <= codes)
check(bad['quality']['automatic_check']=='attention')
check(bad['quality']['review_ids']==[])
# Arbitrary existing issue does not discharge an unlinked registration claim.
r=client.post('/api/reviews/raise',json={'kb_id':'library','doc_id':'quality-bad','topic_version':1,
    'kind':'uncertain','question':'另一个问题','evidence':[{'statement':'另一个口径','locator':'第1页'}]})
check(r.status_code==200)
fresh=client.get('/api/receipt/'+bad['id']).json()['quality']
check(len(fresh['review_ids'])==1 and fresh['automatic_check']=='attention')
_, missing = submit('quality-missing',source_metadata={})
check({'source_kind_missing','locator_missing'} <= {x['code'] for x in missing['quality']['findings']})
conn=connect()
conn.execute('DELETE FROM intake_quality WHERE receipt_id=?',(rid,));conn.commit();conn.close()
check(client.get('/api/receipt/'+rid).json()['quality']['automatic_check']=='not_checked')
conn=connect();check(conn.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0);conn.close()
print('PASS quality end-to-end: persistence, duplicate, warnings, reviews, legacy, zero usage')
