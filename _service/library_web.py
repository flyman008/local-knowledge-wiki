"""Read-only browser; no model calls or knowledge mutations."""
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
import db, domains, authority, review
import archive

def make_router(cfg):
    router = APIRouter()
    def receipts(conn, doc_id):
        rows = conn.execute("""SELECT DISTINCT r.*,c.topic_version FROM receipts r JOIN classifications c ON c.receipt_id=r.id
          WHERE c.kb_id='library' AND c.doc_id=? ORDER BY c.topic_version DESC,r.created_at DESC""", (doc_id,)).fetchall()
        result=[]
        for row in rows:
            item=dict(row)
            for key in ('saved_layers','missing_pages'):
                item[key]=json.loads(item[key]) if item.get(key) else None
            archive.describe_content(item)
            item.pop('raw_path',None); item.pop('dedup_key',None)
            result.append(item)
        return result

    @router.get('/api/library/catalog')
    def catalog():
        conn=db.get_conn()
        try:
            result=[]
            for row in conn.execute("SELECT * FROM topics WHERE kb_id='library' ORDER BY updated_at DESC,id"):
                item=dict(row); item['classification']=domains.get(conn,'library',item['id'])
                item['pending_count']=conn.execute("SELECT count(*) FROM review_items WHERE kb_id='library' AND doc_id=? AND topic_version=? AND status='pending'",(item['id'],item['version'])).fetchone()[0]
                current=next((r for r in receipts(conn,item['id']) if r['topic_version']==item['version']),{})
                for key in ('parse_complete','saved_layers','source_url','content_status'):
                    item[key]=current.get(key)
                body=conn.execute("SELECT body FROM wiki_fts WHERE kb_id='library' AND doc_id=?",(item['id'],)).fetchone()
                item['content_status']='needs_parse' if item['content_status']=='needs_parse' or (body and archive.is_placeholder(body['body'])) else ('readable' if body and body['body'].strip() else 'missing')
                result.append(item)
            counts={r['status']:r['n'] for r in conn.execute("SELECT status,count(*) n FROM receipts WHERE kb_id='library' GROUP BY status")}
            return {'items':result,'receipt_counts':counts,'read_only':True}
        finally: conn.close()

    @router.get('/api/library/detail')
    def detail(doc_id:str):
        conn=db.get_conn()
        try:
            row=conn.execute("SELECT * FROM topics WHERE kb_id='library' AND id=?",(doc_id,)).fetchone()
            if not row: raise HTTPException(404,'知识条目不存在')
            item=dict(row); body=conn.execute("SELECT body FROM wiki_fts WHERE kb_id='library' AND doc_id=?",(doc_id,)).fetchone()
            item.update(body=body['body'] if body else '',body_available=body is not None,classification=domains.get(conn,'library',doc_id),source_metadata=authority.get_metadata(conn,'library',doc_id),review_items=review.context(conn,'library',doc_id),receipts=receipts(conn,doc_id))
            item['content_status']='needs_parse' if row['status']=='needs_parse' or archive.is_placeholder(item['body']) else ('readable' if item['body'].strip() else 'missing')
            if item['content_status']=='needs_parse':
                item.update(body='',body_available=False)
            return item
        finally: conn.close()

    @router.get('/api/library/original')
    def original(receipt_id:str):
        conn=db.get_conn()
        try: row=conn.execute("SELECT raw_path FROM receipts WHERE kb_id='library' AND id=?",(receipt_id,)).fetchone()
        finally: conn.close()
        if not row or not row['raw_path']: raise HTTPException(404,'没有关联原件')
        path=Path(row['raw_path']).resolve()
        roots=[Path(cfg.research_wiki_path).resolve()]+[Path(b.path).resolve() for b in cfg.knowledge_bases if b.id=='library']
        if not any(path.is_relative_to(root/'raw') for root in roots) or not path.is_file(): raise HTTPException(404,'原件不可用')
        return FileResponse(path,filename=path.name,media_type='application/octet-stream',headers={'X-Content-Type-Options':'nosniff'})

    @router.get('/legacy-admin')
    def legacy(): return FileResponse(Path(__file__).parent/'web'/'legacy-admin.html')
    return router
