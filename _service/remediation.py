"""Deterministic, audited repair checks. No LLM or human impersonation."""
import json
import hashlib
from pathlib import Path
import db

PARSE_QUESTION = '解析不完整；缺失部分不能视为已核实，需补材料或限定结论范围'
VERSION_QUESTION = '同标题已有版本：请确认是替代旧口径、不同范围并存，还是无实质变化；这不是已检测到冲突'

def category(item):
    if item['question'] == PARSE_QUESTION:
        return 'client_repair'
    if item['question'] == VERSION_QUESTION:
        return 'version_review'
    return 'human_review' if item['kind'] == 'conflict' else 'evidence_check'

def event(conn, row, status, payload):
    revision = row['revision'] + 1
    conn.execute('UPDATE review_items SET status=?,revision=?,decision=? WHERE id=?',
                 (status,revision,json.dumps(payload,ensure_ascii=False),row['id']))
    conn.execute('INSERT INTO review_events(item_id,revision,payload,created_at) VALUES(?,?,?,?)',
                 (row['id'],revision,json.dumps(payload,ensure_ascii=False),db.now_iso()))

def receipt_for(conn, kb, doc, version):
    return conn.execute('''SELECT r.* FROM classifications c JOIN receipts r ON r.id=c.receipt_id
        WHERE c.kb_id=? AND c.doc_id=? AND c.topic_version=? ORDER BY r.created_at DESC LIMIT 1''',
        (kb,doc,version)).fetchone()

def repair(conn, data):
    row = conn.execute('SELECT * FROM review_items WHERE id=?',(data.get('id'),)).fetchone()
    if not row or row['revision'] != data.get('expected_revision'):
        raise ValueError('事项不存在或 revision 已变化，请重新读取')
    if row['question'] != PARSE_QUESTION or row['kind'] != 'uncertain' or row['status'] != 'pending':
        raise ValueError('仅未解决的解析缺失事项可自动核验')
    topic = conn.execute('SELECT * FROM topics WHERE kb_id=? AND id=?',(row['kb_id'],row['doc_id'])).fetchone()
    old = receipt_for(conn,row['kb_id'],row['doc_id'],row['topic_version'])
    new = receipt_for(conn,row['kb_id'],row['doc_id'],topic['version']) if topic else None
    if not old or not new or new['id'] != data.get('receipt_id') or topic['version'] != row['topic_version']+1:
        raise ValueError('需提交对应主题紧接缺失版本的补读回执；跨版本须重新核对')
    layers = json.loads(new['saved_layers'] or '{}')
    if old['parse_complete'] != 0 or new['parse_complete'] != 1 or json.loads(new['missing_pages'] or '[]') or not all(layers.get(k) for k in ('raw','extract','wiki')):
        raise ValueError('补读三层未齐、仍缺页或原事项不是解析缺失')
    if not old['raw_path'] or not new['raw_path']:
        raise ValueError('缺少可核对的原件')
    digest = lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest()
    if digest(old['raw_path']) != digest(new['raw_path']):
        raise ValueError('原件不同，不能按补读自动关闭')
    qrow = conn.execute('SELECT payload FROM intake_quality WHERE receipt_id=?',(new['id'],)).fetchone()
    quality = json.loads(qrow['payload']) if qrow else {}
    update = quality.get('update',{})
    if any(f.get('code') in {'parse_contradiction','parse_text_warning'} for f in quality.get('findings',[])):
        raise ValueError('解析稿仍有缺失或声明矛盾，不能自动关闭')
    if update.get('kind') != 'editorial_correction' or update.get('base_version') != row['topic_version']:
        raise ValueError('补读必须声明同原件纠错原因及基准版本')
    # The stored extract is bound to the CURRENT wiki via snapshot chain.
    chains = conn.execute('''SELECT e.file_path,w.file_path AS wiki_file FROM snapshots w JOIN snapshots e ON e.id=w.prev_snapshot_id
        WHERE w.kb_id=? AND w.layer='wiki' AND e.layer='extract' ORDER BY w.id DESC''',(row['kb_id'],)).fetchall()
    chain = next((c for c in chains if Path(c['wiki_file']).name == Path(topic['wiki_path']).name),None)
    if not chain:
        raise ValueError('缺少解析快照链')
    extract = Path(chain['file_path']).read_text(encoding='utf-8')
    old_name = row['doc_id'] + (f"_v{row['topic_version']}" if row['topic_version']>1 else '') + '.md'
    old_chain = next((c for c in chains if Path(c['wiki_file']).name == old_name),None)
    if not old_chain or Path(old_chain['file_path']).read_text(encoding='utf-8').strip() == extract.strip():
        raise ValueError('解析稿没有新增补读内容或缺少旧解析快照；仅修改完整性标记不能关闭')
    pages = set(json.loads(old['missing_pages'] or '[]'))
    coverage = data.get('coverage')
    if not pages or not isinstance(coverage,list) or not coverage:
        raise ValueError('需要明确缺失编号及逐项补读依据；未知缺失范围不能自动关闭')
    covered=set()
    for entry in coverage:
        if not isinstance(entry,dict):
            raise ValueError('补读依据格式错误')
        number=entry.get('page')
        quote=entry.get('quote','')
        locator=entry.get('locator','')
        if not isinstance(number,int) or isinstance(number,bool) or number not in pages or not isinstance(quote,str) or len(quote.strip())<12 or quote not in extract or not isinstance(locator,str) or not locator.strip():
            raise ValueError('补读依据须覆盖缺失编号，引用至少12字符且在已保存解析稿中存在，并注明原件位置')
        covered.add(number)
    if covered != pages:
        raise ValueError('尚未逐项覆盖所有缺失编号')
    actor=data.get('checked_by','')
    if not isinstance(actor,str) or not actor.strip():
        raise ValueError('需要如实记录补读客户端')
    payload={'action':'repair_verified','reason':update.get('reason'),'basis':'same_raw_version_layers_and_extract_quotes',
             'receipt_id':new['id'],'coverage':coverage,'confirmed_by':'service:repair-v1','checked_by':actor,
             'semantic_verified':False,'conclusion':'对应缺失已由客户端补读，服务机械核验通过；不代表全文事实认证'}
    event(conn,row,'resolved',payload)
    closed=[row['id']]
    # Suppress only the generated version task for a pure repair, never business questions.
    other=conn.execute("SELECT * FROM review_items WHERE kb_id=? AND doc_id=? AND status='pending'",(row['kb_id'],row['doc_id'])).fetchall()
    if all(r['topic_version']==topic['version'] and r['question']==VERSION_QUESTION for r in other):
        for r in other:
            event(conn,r,'resolved',{**payload,'action':'repair_version_verified','conclusion':'同原件补读修订，版本记录保留'})
            closed.append(r['id'])
    return {'closed_ids':closed,'receipt_id':new['id'],'semantic_verified':False,
            'remaining_pending':conn.execute("SELECT count(*) FROM review_items WHERE kb_id=? AND doc_id=? AND status='pending'",(row['kb_id'],row['doc_id'])).fetchone()[0]}

def reopen(conn,data):
    row=conn.execute('SELECT * FROM review_items WHERE id=?',(data.get('id'),)).fetchone()
    if not row or row['revision'] != data.get('expected_revision'):
        raise ValueError('事项不存在或 revision 已变化')
    decision=json.loads(row['decision'] or '{}')
    if row['status']!='resolved' or decision.get('action') not in {'repair_verified','repair_version_verified'}:
        raise ValueError('这里只能重新打开自动核验关闭的技术事项')
    reason=data.get('reason')
    if not isinstance(reason,str) or not reason.strip():
        raise ValueError('需要重新打开原因')
    event(conn,row,'pending',{'action':'repair_reopened','reason':reason,'previous_decision':decision})
    return {'id':row['id'],'status':'pending','revision':row['revision']+1}
