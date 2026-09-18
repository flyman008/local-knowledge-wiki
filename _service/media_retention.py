"""Client-declared media acceptance, immutable shared objects and delayed cleanup.

No OCR, transcription, network fetching or model calls. Existing raw is untouched
until an explicitly staged plan has aged seven days and passes the gates again.
"""
import hashlib
import io
import json
import math
import os
import threading
import uuid
from functools import wraps
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import db
from storage import Storage

LOCK = threading.RLock()

def serialized(fn):
    @wraps(fn)
    def call(*args, **kwargs):
        with LOCK: return fn(*args, **kwargs)
    return call
IMAGE = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.tif', '.tiff'}
VIDEO = {'.mp4', '.mov', '.mkv', '.webm', '.avi', '.m4v'}
AUDIO = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg', '.opus'}
MAX_ENTRY = 256 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024

def digest(data): return hashlib.sha256(data).hexdigest()
def now(): return datetime.now(timezone.utc)
def root(cfg): return Storage(cfg).kb_path('library') / 'media'

def init(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS media_plans (
        receipt_id TEXT PRIMARY KEY, state TEXT NOT NULL, payload TEXT NOT NULL,
        updated_at TEXT NOT NULL)''')

def read(conn, receipt_id):
    # Old databases/read-only portal must work before the first media submission.
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='media_plans'").fetchone():
        return None
    row = conn.execute('SELECT payload FROM media_plans WHERE receipt_id=?', (receipt_id,)).fetchone()
    return json.loads(row['payload']) if row else None

def save(conn, plan):
    conn.execute('INSERT OR REPLACE INTO media_plans VALUES(?,?,?,?)',
                 (plan['receipt_id'], plan['state'], json.dumps(plan, ensure_ascii=False), now().isoformat()))

def safe_name(name):
    if not isinstance(name, str) or not name or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('不安全的媒体文件名')
    p = PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts or name.startswith('__knowledge_media__/'):
        raise ValueError('媒体路径越界或使用保留目录')
    return name

def unpack(data):
    result = {}
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        total = 0
        for item in z.infolist():
            if item.is_dir(): continue
            name = safe_name(item.filename)
            total += item.file_size
            if len(result) >= 5000 or item.file_size > MAX_ENTRY or total > MAX_TOTAL:
                raise ValueError('采集包超过媒体处理上限，保持原件')
            if name in result or (item.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('采集包含重名或符号链接，保持原件')
            result[name] = z.read(item)
    return result

def kind(name, data):
    ext = Path(name).suffix.lower()
    if ext in IMAGE or ext == '.gif':
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            if getattr(image, 'n_frames', 1) > 1: return 'animation'
        return 'image'
    if ext in VIDEO: return 'video'
    if ext in AUDIO: return 'audio'
    return 'other'

def compress_image(data):
    from PIL import Image
    with Image.open(io.BytesIO(data)) as src:
        src.load()
        if src.width * src.height > 40_000_000 or src.mode not in ('RGB', 'RGBA', 'L', 'LA', 'P'):
            return data, 'original', '保持原格式：尺寸或色彩模式不适合试压'
        # Preserve unusual metadata conservatively, rather than silently dropping it.
        if set(src.info) - {'icc_profile', 'exif', 'xmp', 'dpi', 'transparency', 'jfif', 'jfif_version', 'jfif_unit', 'jfif_density'}:
            return data, 'original', '保持原格式：含额外元数据'
        meta = {k: src.info[k] for k in ('icc_profile', 'exif', 'xmp') if src.info.get(k)}
        out = io.BytesIO()
        src.save(out, format='WEBP', lossless=True, exact=True, method=4, **meta)
        candidate = out.getvalue()
        with Image.open(io.BytesIO(candidate)) as dst:
            def same(k, v):
                got = dst.info.get(k)
                if k == 'exif' and isinstance(got, bytes) and isinstance(v, bytes):
                    return got.removeprefix(b'Exif\0\0') == v.removeprefix(b'Exif\0\0')
                return got == v
            valid = dst.size == src.size and dst.convert('RGBA').tobytes() == src.convert('RGBA').tobytes()
            valid = valid and all(same(k, v) for k, v in meta.items())
        if valid and len(candidate) <= len(data) * .95:
            return candidate, 'webp', '无损压缩；像素、尺寸和ICC/EXIF/XMP校验通过'
        return data, 'original', '保持原格式：节省不足5%或校验未通过'

def object_path(cfg, hash_value):
    if len(hash_value) != 64 or any(c not in '0123456789abcdef' for c in hash_value):
        raise ValueError('非法对象哈希')
    base = (root(cfg) / 'objects').resolve()
    p = (base / hash_value[:2] / hash_value).resolve()
    if not p.is_relative_to(base): raise ValueError('对象路径越界')
    return p

def put_object(cfg, data):
    key = digest(data); path = object_path(cfg, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if digest(path.read_bytes()) != key: raise ValueError('共享对象校验失败，停止')
    else:
        temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        temp.write_bytes(data); os.replace(temp, path)
    return key

def raw_path(cfg, receipt):
    p = Path(receipt['raw_path'] or '').resolve()
    base = Storage(cfg).raw_dir('library').resolve()
    if not p.is_relative_to(base) or p == base: raise ValueError('原件不在本库raw目录')
    return p

def acceptance(conn, cfg, rid):
    r = conn.execute("SELECT * FROM receipts WHERE id=? AND kb_id='library'", (rid,)).fetchone()
    if not r: raise ValueError('回执不存在')
    if r['status'] != 'done' or r['parse_complete'] != 1 or json.loads(r['missing_pages'] or '[]'):
        raise ValueError('解析未完整，不允许清理')
    layers = json.loads(r['saved_layers'] or '{}')
    if not all(layers.get(k) for k in ('raw', 'extract', 'wiki')): raise ValueError('三层未齐备')
    e = conn.execute('SELECT * FROM evidence_metadata WHERE receipt_id=?', (rid,)).fetchone()
    if not e: raise ValueError('仅接受prepared的三层回执')
    if conn.execute("SELECT 1 FROM review_items WHERE kb_id='library' AND doc_id=? AND status='pending' LIMIT 1", (e['doc_id'],)).fetchone():
        raise ValueError('仍有未解决事项，保留原件')
    # Resolve immutable extract through this receipt's wiki version, not the latest title.
    suffix = f"{e['doc_id']}_v{e['topic_version']}.md" if e['topic_version'] > 1 else f"{e['doc_id']}.md"
    wiki = Storage(cfg).inbox_wiki_dir('library') / suffix
    snap = conn.execute("SELECT x.file_path FROM snapshots w JOIN snapshots x ON w.prev_snapshot_id=x.id WHERE w.layer='wiki' AND w.file_path=?", (str(wiki),)).fetchone()
    if not snap or not wiki.is_file(): raise ValueError('缺少对应版本的解析稿或成果')
    extract = Path(snap['file_path']).resolve()
    if not extract.is_relative_to(Storage(cfg).kb_path('library').resolve()) or not extract.is_file():
        raise ValueError('解析稿不可用')
    return r, extract.read_text(encoding='utf-8')

def quote(text, value):
    return isinstance(value, str) and len(value.strip()) >= 12 and value in text

def valid_url(value):
    try:
        u = urlparse(value)
        return u.scheme in ('http', 'https') and bool(u.hostname) and not u.username
    except (ValueError, TypeError): return False

def prepare(cfg, payload, derivatives=None):
    """Register a plan only. Never schedules or deletes automatically on submission."""
    with LOCK:
        conn = db.get_conn()
        try:
            init(conn)
            rid = payload.get('receipt_id', '')
            old = read(conn, rid)
            if old and old['state'] in ('staged', 'purged'):
                raise ValueError('先取消待清理计划；已清理计划不可重建')
            r, extract = acceptance(conn, cfg, rid)
            path = raw_path(cfg, r)
            if path.stat().st_size > MAX_TOTAL: raise ValueError('原件过大，保持原件')
            data = path.read_bytes()
            if digest(data) != payload.get('raw_sha256'): raise ValueError('原件哈希不匹配')
            if path.suffix.lower() not in IMAGE | VIDEO | AUDIO | {'.gif', '.zip'}:
                raise ValueError('不拆改PDF/PPT/Word等文档原件')
            members = unpack(data) if path.suffix.lower() == '.zip' else {path.name: data}
            extra = unpack(derivatives) if derivatives else {}
            if set(extra) & set(members): raise ValueError('关键帧与原包条目重名')
            records = payload.get('items', [])
            entries = {x['name']: x for x in records}
            if len(entries) != len(records): raise ValueError('媒体清单重复')
            kinds = {name: kind(name, value) for name, value in members.items()}
            expected = {n for n, k in kinds.items() if k != 'other'}
            if not expected or set(entries) != expected: raise ValueError('清单必须精确覆盖所有图片/音频/视频/动图')
            blockers = []; retained = {}; details = []; used_frames = set()
            if not payload.get('checked_by'): blockers.append('未记录验收客户端')
            for name, content in members.items():
                k = kinds[name]
                if k == 'other':
                    retained[name] = (content, 'original'); continue
                m = entries[name]
                if m.get('sha256') != digest(content): raise ValueError('媒体哈希不匹配：' + name)
                if m.get('complete') is not True or not quote(extract, m.get('quote')):
                    blockers.append(name + '：缺少完整解析声明或解析稿中的定位引用')
                item = {'name': name, 'kind': k, 'original_sha256': digest(content), 'original_bytes': len(content),
                        'source_url': m.get('source_url'), 'locator': m.get('locator'), 'quote': m.get('quote')}
                if not m.get('locator'): blockers.append(name + '：缺少原文位置')
                if k == 'image':
                    optimized, encoding, reason = compress_image(content)
                    retained[name] = (optimized, encoding)
                    item.update(action='retain', reason=reason, retained_bytes=len(optimized))
                else:
                    if not valid_url(m.get('source_url')) or m.get('refetchable') is not True:
                        blockers.append(name + '：没有已核验的可回访来源')
                    if not quote(extract, m.get('timeline_quote')):
                        blockers.append(name + '：缺少带时间定位的转写/动作说明引用')
                    if m.get('timeline_checked') is not True:
                        blockers.append(name + '：未声明时间线已核对')
                    if k in ('video', 'animation'):
                        frames = m.get('keyframes', [])
                        times = []
                        for frame in frames:
                            fn = frame['name']; t = frame.get('seconds')
                            if not isinstance(t, (int, float)) or not math.isfinite(t) or t < 0:
                                raise ValueError('关键帧时间无效')
                            times.append(t)
                            raw = extra.get(fn, members.get(fn))
                            if raw is None or kind(fn, raw) != 'image': raise ValueError('关键帧不存在或不是静态图')
                            optimized, encoding, _ = compress_image(raw)
                            retained[fn] = (optimized, encoding); used_frames.add(fn)
                        if not frames or times != sorted(set(times)) or not quote(extract, m.get('visual_quote')):
                            blockers.append(name + '：关键帧、顺序或画面说明缺失')
                        item['keyframes'] = frames
                    item.update(action='remove_after_grace', reason='验收声明及引用齐备后，保留文本/关键帧/来源，清理动态原件')
                details.append(item)
            if set(extra) != used_frames & set(extra): raise ValueError('衍生包只能包含被引用的关键帧')
            # Also protects sole local image originals / ZIP originals.
            if not valid_url(payload.get('source_url')) or payload.get('refetchable') is not True:
                blockers.append('没有可回访的包/原件来源；仅本地原件不清理')
            try:
                checked = datetime.fromisoformat(payload['source_checked_at'])
                age = now() - checked
                if age < timedelta(0) or age > timedelta(days=7): raise ValueError()
            except (KeyError, ValueError, TypeError):
                blockers.append('缺少最近7天内的来源核验时间（含时区）')
            plan = {'receipt_id': rid, 'state': 'blocked' if blockers else 'ready', 'blockers': blockers,
                    'raw_sha256': digest(data), 'extract_sha256':digest(extract.encode('utf-8')), 'original_bytes': len(data), 'source_url': payload.get('source_url'),
                    'source_checked_at': payload.get('source_checked_at'), 'checked_by': payload.get('checked_by'),
                    'acceptance_basis': 'client_declaration_and_mechanical_checks_not_semantic_verification',
                    'items': details, 'objects': [], 'history': [{'action': 'prepare', 'at': now().isoformat()}]}
            if not blockers:
                for name, (content, encoding) in retained.items():
                    plan['objects'].append({'name': name, 'sha256': put_object(cfg, content), 'bytes': len(content), 'encoding': encoding})
                plan['retained_bytes_before_zip'] = sum(x['bytes'] for x in plan['objects'])
            save(conn, plan); conn.commit()
            return plan
        finally: conn.close()

def verify_objects(cfg, plan):
    for obj in plan['objects']:
        p = object_path(cfg, obj['sha256'])
        if not p.is_file() or digest(p.read_bytes()) != obj['sha256']:
            raise ValueError('保留对象缺失或损坏，禁止清理')

def change(cfg, rid, action):
    with LOCK:
        conn = db.get_conn()
        try:
            init(conn); plan = read(conn, rid)
            if not plan: raise ValueError('先提交媒体清单')
            if action == 'stage':
                if plan['state'] == 'staged': return plan
                if plan['state'] not in ('ready', 'cancelled'): raise ValueError('计划未通过或已清理')
                r, extract = acceptance(conn, cfg, rid)
                if digest(extract.encode('utf-8')) != plan['extract_sha256']: raise ValueError('解析稿已变化，请重新验收')
                if digest(raw_path(cfg, r).read_bytes()) != plan['raw_sha256']: raise ValueError('原件已改变')
                verify_objects(cfg, plan)
                plan.update(state='staged', purge_after=(now() + timedelta(days=7)).isoformat())
            elif action == 'cancel':
                if plan['state'] == 'purged': raise ValueError('原件已清理，不能假称本地可恢复；请访问来源')
                plan.update(state='cancelled', purge_after=None)
            else: raise ValueError('未知媒体操作')
            plan['history'].append({'action': action, 'at': now().isoformat()})
            save(conn, plan); conn.commit(); return plan
        finally: conn.close()

def sweep(cfg):
    """Only remove explicitly staged raw; current acceptance and shared refs veto."""
    with LOCK:
        conn = db.get_conn(); results = []
        try:
            init(conn); conn.commit(); conn.execute('BEGIN IMMEDIATE')
            for row in conn.execute("SELECT receipt_id FROM media_plans WHERE state='staged'").fetchall():
                p = read(conn, row['receipt_id'])
                if now() < datetime.fromisoformat(p['purge_after']): continue
                try:
                    r, _ = acceptance(conn, cfg, p['receipt_id']); path = raw_path(cfg, r)
                    # All receipts sharing this exact raw must have completed their grace period.
                    refs = [ref for ref in conn.execute('SELECT id,raw_path FROM receipts WHERE raw_path IS NOT NULL').fetchall()
                            if Path(ref['raw_path']).resolve() == path]
                    plans = []
                    for ref in refs:
                        other = read(conn, ref['id'])
                        if not other or other['state'] not in ('staged', 'purged'):
                            raise ValueError('还有其他回执引用原件且未批准清理')
                        if other['raw_sha256'] != p['raw_sha256']:
                            raise ValueError('共享原件的身份不一致')
                        if other['state'] == 'staged' and now() < datetime.fromisoformat(other['purge_after']):
                            raise ValueError('共享原件仍在恢复期')
                        _, extract = acceptance(conn, cfg, ref['id'])
                        if digest(extract.encode('utf-8')) != other['extract_sha256']: raise ValueError('解析稿已变化')
                        verify_objects(cfg, other); plans.append(other)
                    if path.exists():
                        if digest(path.read_bytes()) != p['raw_sha256']: raise ValueError('原件哈希改变')
                        path.unlink()  # explicit resolved file only; never recurse
                    for other in plans:
                        other.update(state='purged', purged_at=now().isoformat(), last_error=None)
                        other['history'].append({'action': 'purge', 'at': now().isoformat()})
                        save(conn, other)
                    results.append({'receipt_id': p['receipt_id'], 'state': 'purged'})
                except (ValueError, OSError) as exc:
                    p['last_error'] = str(exc); save(conn, p)
                    results.append({'receipt_id': p['receipt_id'], 'state': 'held', 'reason': str(exc)})
            conn.commit(); return results
        finally: conn.close()

def retained_zip(cfg, plan):
    verify_objects(cfg, plan)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for obj in plan['objects']:
            z.writestr(safe_name(obj['name']), object_path(cfg, obj['sha256']).read_bytes())
        z.writestr('__knowledge_media__/manifest.json', json.dumps(plan, ensure_ascii=False, indent=2))
    return buf.getvalue()

def make_router(cfg):
    from fastapi import APIRouter, File, Form, UploadFile, HTTPException
    from fastapi.responses import Response
    router = APIRouter()

    @router.post('/api/media/prepare')
    async def plan(payload: str = Form(...), file: UploadFile | None = File(None)):
        raw = await file.read(MAX_TOTAL + 1) if file else None
        if raw and len(raw) > MAX_TOTAL: raise ValueError('关键帧包过大')
        return prepare(cfg, json.loads(payload), raw)

    @router.get('/api/media/status')
    def status(receipt_id: str):
        conn = db.get_conn()
        try: return read(conn, receipt_id) or {'state': 'unmanaged', 'reason': '未提交媒体验收清单，原件保持不动'}
        finally: conn.close()

    @router.get('/api/media/inventory')
    def inventory(receipt_id: str):
        conn = db.get_conn()
        try: r = conn.execute("SELECT * FROM receipts WHERE id=? AND kb_id='library'", (receipt_id,)).fetchone()
        finally: conn.close()
        if not r: raise ValueError('回执不存在')
        path = raw_path(cfg, r)
        if not path.is_file() or path.stat().st_size > MAX_TOTAL: raise ValueError('原件不存在或过大')
        if path.suffix.lower() not in IMAGE | VIDEO | AUDIO | {'.gif', '.zip'}: raise ValueError('文档原件不拆改')
        raw = path.read_bytes()
        members = unpack(raw) if path.suffix.lower()=='.zip' else {path.name:raw}
        return {'receipt_id':receipt_id, 'raw_sha256':digest(raw), 'items':[
            {'name':n,'kind':kind(n,d),'sha256':digest(d),'bytes':len(d)} for n,d in members.items()]}

    @router.post('/api/media/action')
    def action(payload: dict): return change(cfg, payload['receipt_id'], payload['action'])

    @router.get('/api/media/retained')
    def download(receipt_id: str):
        p = status(receipt_id)
        if not p.get('objects'): raise HTTPException(404, '没有已核验保存的媒体包')
        return Response(retained_zip(cfg, p), media_type='application/zip',
                        headers={'Content-Disposition': 'attachment; filename="retained-media.zip"'})
    return router
