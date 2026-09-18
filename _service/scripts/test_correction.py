"""Correction exemption: isolated DB/files, no models."""
from test_taxonomy import client, check, connect

def put(title, text, raw=b'original', **kw):
    data={'title':title,'extract_text':'page 1','wiki_text':text, **kw}
    return client.post('/api/archive_full_file', data=data, files={'file':('original.txt',raw,'text/plain')})

def receipt(r):
    check(r.status_code==200)
    return client.get('/api/receipt/'+r.json()['receipt_id']).json()

receipt(put('correction','typo'))
r=put('correction','fixed',correction_reason='typo; page 1',base_version=1)
d=receipt(r)
check(d['quality']['update']['version_review_skipped'] is True)
check(d['topic_version']==2 and not d['review_items'])
check(receipt(put('correction','fixed',correction_reason='typo; page 1',base_version=1))['id']==d['id'])
check(put('correction','another',correction_reason='typo',base_version=1).status_code==400)
receipt(put('different','old'))
d2=receipt(put('different','new',raw=b'changed',correction_reason='claimed fix',base_version=1))
check(not d2['quality']['update']['version_review_skipped'] and len(d2['review_items'])==1)
receipt(put('pending','old'))
q=client.post('/api/reviews/raise',json={'kb_id':'library','doc_id':'pending','topic_version':1,'kind':'uncertain','question':'price?', 'evidence':[{'statement':'unclear','locator':'page 1'}]})
check(q.status_code==200)
d3=receipt(put('pending','fixed',correction_reason='typo',base_version=1))
check(not d3['quality']['update']['version_review_skipped'])
check(all(x['status']=='pending' for x in d3['review_items']))
receipt(put('ordinary','old'))
check(len(receipt(put('ordinary','new'))['review_items'])==1)
conn=connect()
check(conn.execute("SELECT count(*) FROM snapshots WHERE kb_id='library' AND layer='wiki'").fetchone()[0]>=2)
check(conn.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0)
conn.close()
print('PASS correction: eligible, duplicate, stale rejection, changed source, pending preservation, ordinary update')
