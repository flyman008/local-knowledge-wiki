"""Date semantics and HTTP catalog, isolated DB; zero model calls."""
from test_taxonomy import client, check, connect
from datetime import date
import material_dates as md

check(md.bounds('2024-02')[1].isoformat() == '2024-02-29')
check(md.bounds('2025-02-29') is None)
check(md.bounds('2026-13') is None)
check(md.bounds('采集于2026-01-01') is None)
check(md.describe({'effective_at':'2020-01-01'})['value'] is None)
check(not md.describe({'published_at':'2025'},date(2026,6,1))['older_than_year'])
check(md.describe({'published_at':'2024'},date(2026,6,1))['older_than_year'])
check(md.describe({'published_at':'2027-01'},date(2026,6,1))['future'])
check(md.findings({'date_status':'unknown','date_note':'检查全文无日期'}) == [])
check(any(x['code']=='source_date_unrecorded' for x in md.findings({})))
check(any(x['code']=='source_date_evidence' for x in md.findings({'published_at':'2026-01'})))
for name,metadata in [('dated',{'document_date':'2020-01','date_evidence':'document_date：封面2020年1月'}),('undated',{'date_status':'unknown','date_note':'原文没有日期'})]:
    response=client.post('/api/archive_full',json={'title':name,'extract_text':'测试原文','wiki_text':'测试内容','source_metadata':dict(source_kind='other',locator='全文',**metadata),'classification':{'scopes':['external'],'tags':['AI']}})
    check(response.status_code==200)
    receipt=client.get('/api/receipt/'+response.json()['receipt_id']).json()
    check(receipt['quality']['automatic_check']=='passed')
catalog=client.get('/api/library/catalog').json()['items']
dated=next(x for x in catalog if x['name']=='dated')
check(dated['material_date']['value']=='2020-01')
check(dated['material_date']['older_than_year'])
detail=client.get('/api/library/detail',params={'doc_id':dated['id']}).json()
check(detail['material_date']==dated['material_date'])
from backfill_material_dates import propose
check(propose({'published_at':'材料未标注日期'},None)['date_status']=='unknown')
check(propose({'published_at':'封面印制 2026-08'},None)['document_date']=='2026-08')
conn=connect();check(conn.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0);conn.close()
print('PASS material dates: precision, invalid dates, unknown, effective-only, evidence, catalog/detail, backfill, zero model usage')
