"""Version-bound human adjudication. No model calls or evidence rewriting."""
import json
import uuid
import db
import domains

def _json(value):
    return json.dumps(value, ensure_ascii=False)

def _text(data, key):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{key} 必须是非空文本')
    return value.strip()

def add(conn, kb_id, doc_id, version, kind, question, evidence):
    if kind not in {'conflict', 'uncertain'} or not question.strip() or not evidence:
        raise ValueError('必须提供问题类型、问题和可回查依据')
    item_id = 'review_' + uuid.uuid4().hex
    conn.execute('INSERT INTO review_items(id,kb_id,doc_id,topic_version,kind,question,evidence,created_at) VALUES(?,?,?,?,?,?,?,?)',
                 (item_id,kb_id,doc_id,version,kind,question,_json(evidence),db.now_iso()))
    return item_id

def context(conn, kb_id, doc_id):
    topic = conn.execute('SELECT version FROM topics WHERE kb_id=? AND id=?',(kb_id,doc_id)).fetchone()
    rows = conn.execute('SELECT * FROM review_items WHERE kb_id=? AND doc_id=? ORDER BY created_at',(kb_id,doc_id)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item['evidence'] = json.loads(item['evidence'])
        item['decision'] = json.loads(item['decision']) if item['decision'] else None
        item['applies_to_current_version'] = bool(topic and topic['version'] == item['topic_version'])
        result.append(item)
    return result

def execute(operation, data):
    conn = db.get_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')
        if operation == 'raise':
            kb, doc = _text(data,'kb_id'), _text(data,'doc_id')
            row = conn.execute('SELECT version FROM topics WHERE kb_id=? AND id=?',(kb,doc)).fetchone()
            if not row or data.get('topic_version') != row['version']:
                raise ValueError('主题不存在或版本已变化，请重新读取')
            evidence = data.get('evidence')
            if not isinstance(evidence,list) or not all(isinstance(e,dict) and isinstance(e.get('locator'),str) and e['locator'].strip() and isinstance(e.get('statement'),str) and e['statement'].strip() for e in evidence):
                raise ValueError('evidence 必须包含 statement 和 locator；不得虚构依据')
            result = {'id':add(conn,kb,doc,row['version'],_text(data,'kind'),_text(data,'question'),evidence)}
        elif operation == 'decide':
            item_id = _text(data,'id')
            row = conn.execute('SELECT * FROM review_items WHERE id=?',(item_id,)).fetchone()
            if not row or data.get('expected_revision') != row['revision']:
                raise ValueError('确认项不存在或已变更，请重新读取')
            current = conn.execute('SELECT version FROM topics WHERE kb_id=? AND id=?',(row['kb_id'],row['doc_id'])).fetchone()
            if not current or current['version'] != row['topic_version']:
                raise ValueError('材料已有新版，旧版裁决不能用于新版，请重新提出确认项')
            action = _text(data,'action')
            if action not in {'adopt','coexist','supersede','unresolved','reopen'}:
                raise ValueError('未知裁决动作')
            decision = {k:_text(data,k) for k in ('reason','scope','confirmed_by')}
            if data.get('user_confirmed') is not True:
                raise ValueError('必须先向用户展示并获得明确确认')
            decision['action'] = action
            decision['identity_verified'] = False
            if action in {'adopt','coexist','supersede'}:
                decision['conclusion'] = _text(data,'conclusion')
                decision['basis'] = _text(data,'basis')
            if action == 'supersede':
                decision['effective_at'] = _text(data,'effective_at')
            status = 'pending' if action in {'unresolved','reopen'} else 'resolved'
            conn.execute('UPDATE review_items SET status=?, revision=revision+1,decision=? WHERE id=?',(status,_json(decision),item_id))
            conn.execute('INSERT INTO review_events(item_id,revision,payload,created_at) VALUES(?,?,?,?)',(item_id,row['revision']+1,_json(decision),db.now_iso()))
            result = {'id':item_id,'status':status,'revision':row['revision']+1}
        elif operation == 'propose-policy':
            item_id = _text(data,'id')
            row = conn.execute('SELECT status FROM review_items WHERE id=?',(item_id,)).fetchone()
            if not row or row['status'] != 'resolved':
                raise ValueError('仅已解决确认项可提出策略建议')
            pid = 'policy_' + uuid.uuid4().hex
            conn.execute('INSERT INTO policy_proposals(id,item_id,rule,scope,updated_at) VALUES(?,?,?,?,?)',(pid,item_id,_text(data,'rule'),_text(data,'scope'),db.now_iso()))
            result = {'id':pid,'status':'proposed','revision':1}
        elif operation == 'policy-state':
            pid = _text(data,'id')
            row = conn.execute('SELECT * FROM policy_proposals WHERE id=?',(pid,)).fetchone()
            if not row or data.get('expected_revision') != row['revision']:
                raise ValueError('策略不存在或已变更')
            action = _text(data,'action')
            if action not in {'active','revoked'} or data.get('user_confirmed') is not True:
                raise ValueError('策略启用/撤销需要单独明确确认')
            event = {k:_text(data,k) for k in ('confirmed_by','reason')}
            event.update(action=action,identity_verified=False)
            conn.execute('UPDATE policy_proposals SET status=?,revision=revision+1,updated_at=? WHERE id=?',(action,db.now_iso(),pid))
            conn.execute('INSERT INTO review_events(item_id,revision,payload,created_at) VALUES(?,?,?,?)',(pid,row['revision']+1,_json(event),db.now_iso()))
            result = {'id':pid,'status':action,'revision':row['revision']+1}
        else:
            raise ValueError('未知操作')
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def listing(kb_id=None):
    conn = db.get_conn()
    try:
        members = domains.members_for(kb_id)
        rows = conn.execute('SELECT DISTINCT kb_id,doc_id FROM review_items').fetchall()
        items = [i for r in rows if not members or r['kb_id'] in members for i in context(conn,r['kb_id'],r['doc_id'])]
        return {'items':items,'policies':[dict(r) for r in conn.execute('SELECT * FROM policy_proposals')],
                'events':[dict(r) for r in conn.execute('SELECT * FROM review_events ORDER BY id')]}
    finally:
        conn.close()

def policies(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM policy_proposals WHERE status='active'")]
