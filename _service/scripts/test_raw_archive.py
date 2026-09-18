"""Isolated regression for raw-only archive; never touches live data/models."""
from test_taxonomy import client,check,connect

def archive(title='sheet',raw=b'opaque-xlsx'):
    r=client.post('/api/archive_file',data={'title':title},files={'file':('sheet.xlsx',raw,'application/octet-stream')})
    check(r.status_code==200);return r.json()

a=archive()
check(a['content_status']=='needs_parse' and a['complete'] is False)
check(a['saved_layers']=={'raw':True,'extract':False,'wiki':False})
check(client.get('/api/receipt/'+a['receipt_id']).json()['content_status']=='needs_parse')
check(archive()['receipt_id']==a['receipt_id'])
c=connect();check(c.execute("SELECT count(*) FROM wiki_fts WHERE doc_id=?",(a['doc_id'],)).fetchone()[0]==0);c.close()
d=client.get('/api/library/detail',params={'doc_id':a['doc_id']}).json()
check(not d['body_available'] and d['content_status']=='needs_parse')
client.post('/api/archive',json={'title':'existing','text':'valuable-body'})
b=archive('existing')
check(b['doc_id']!='existing')
check(client.get('/api/library/detail?doc_id=existing').json()['body']=='valuable-body')
# Same-original correction of raw-only archive goes through prepared with version history.
p=client.post('/api/archive_full_file',data={'title':'sheet','extract_text':'A1 exact','wiki_text':'A1 exact','base_version':'1','correction_reason':'补齐单元格'},files={'file':('sheet.xlsx',b'opaque-xlsx','application/octet-stream')})
check(p.status_code==200 and p.json()['version']==2)
r=client.get('/api/receipt/'+p.json()['receipt_id']).json()
check(r['quality']['update']['version_review_skipped'] is True)
check(client.get('/api/library/detail?doc_id=sheet').json()['content_status']=='readable')
c=connect();check(c.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0);c.close()
print('PASS raw-only archive, duplicate, no placeholder index, existing knowledge preserved, correction, zero model calls')
