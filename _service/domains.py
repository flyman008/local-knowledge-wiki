"""One storage namespace; subject scopes and topic tags are independent."""
import json


def domain_for(kb_id):
    return 'library' if kb_id == 'library' else None


def members_for(kb_id):
    return (kb_id,) if kb_id else ()


def describe_record(record):
    result = dict(record)
    result["knowledge_domain"] = domain_for(result.get("target_kb_id") or result.get("kb_id"))
    # No existing material-level publication approval is available here.
    result["publication_status"] = "unreviewed"
    return result


def catalog():
    return [{'id':'library','domain':'统一知识库','access':'private'}]

def normalize(value=None):
    value = {} if value is None else value
    if not isinstance(value,dict) or set(value)-{'scopes','tags'}:
        raise ValueError('classification 仅支持 scopes/tags 数组')
    result = {}
    for key in ('scopes','tags'):
        values = value.get(key,[])
        if not isinstance(values,list) or any(not isinstance(v,str) or not v.strip() for v in values):
            raise ValueError(f'{key} 必须是非空字符串数组')
        values = [v.strip() for v in values]
        if key == 'scopes' and set(values)-{'weimob','external'}:
            raise ValueError('scopes 仅支持 weimob/external，可同时选择')
        if key == 'tags':
            values = [v.upper() if v.lower()=='ai' else 'SaaS' if v.lower()=='saas' else v for v in values]
        result[key] = sorted(set(values))
    return result

def save(conn, receipt_id, value, kb_id, doc_id=None, version=None):
    value=normalize(value)
    conn.execute('INSERT OR REPLACE INTO classifications(receipt_id,kb_id,doc_id,topic_version,payload) VALUES(?,?,?,?,?)',
                 (receipt_id,kb_id,doc_id,version,json.dumps(value,ensure_ascii=False)))

def bind(conn,receipt_id,kb_id,doc_id):
    topic=conn.execute('SELECT version FROM topics WHERE kb_id=? AND id=?',(kb_id,doc_id)).fetchone()
    if topic:
        conn.execute('UPDATE classifications SET doc_id=?,topic_version=? WHERE receipt_id=?',(doc_id,topic['version'],receipt_id))

def get(conn,kb_id,doc_id):
    rows=conn.execute('SELECT c.payload FROM classifications c JOIN topics t ON t.kb_id=c.kb_id AND t.id=c.doc_id AND t.version=c.topic_version WHERE c.kb_id=? AND c.doc_id=?',(kb_id,doc_id)).fetchall()
    result={'scopes':[],'tags':[]}
    for row in rows:
        v=json.loads(row['payload'])
        for key in result:
            result[key]=sorted(set(result[key]+v[key]))
    result['classification_status']='classified' if result['scopes'] else 'pending'
    return result

def filter_sql(scope=None,tag=None):
    if scope and scope not in {'weimob','external'}:
        raise ValueError('未知资料范围')
    terms=[]; args=[]
    for key,value in (('scopes',scope),('tags',tag)):
        if value:
            if key=='tags': value=normalize({'tags':[value]})['tags'][0]
            terms.append("EXISTS (SELECT 1 FROM classifications c JOIN topics t ON t.kb_id=c.kb_id AND t.id=c.doc_id AND t.version=c.topic_version, json_each(c.payload, '$."+key+"') j WHERE c.kb_id=wiki_fts.kb_id AND c.doc_id=wiki_fts.doc_id AND j.value=?)")
            args.append(value)
    return (' AND '.join(terms) or '1=1'),args
