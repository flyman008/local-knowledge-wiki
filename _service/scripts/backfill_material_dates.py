"""Conservative, local-only metadata repair. Dry run unless --apply.

Reads original HTML article headers in existing ZIPs and explicit legacy notes.
Never guesses from filenames, body mentions, file dates, or acquisition dates.
"""
import argparse
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import sys
import zipfile
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from material_dates import bounds


def propose(old, raw_path):
    new = dict(old)
    original_pub = old.get('published_at', '')
    if '未标注日期' in original_pub:
        new.pop('published_at', None)
        new.update(date_status='unknown', date_note=original_pub)
    elif '封面' in original_pub and re.search(r'\d{4}-\d{2}', original_pub):
        new.pop('published_at', None)
        new.update(document_date=re.search(r'\d{4}-\d{2}', original_pub)[0], date_status='known',
                   date_note='依据既有封面日期记录重新归类，未重新验证封面图像',
                   date_evidence='document_date：旧来源记录原话：'+original_pub)
    if '封面' in old.get('document_date', ''):
        value = re.search(r'\d{4}-\d{2}', old['document_date'])
        if value:
            new['document_date'] = value[0]
            new['date_evidence'] = 'document_date：旧来源记录原话：'+old['document_date']
            if new.get('published_at') == value[0]:
                new.pop('published_at', None)
            new['date_status'] = 'known'
    if new.get('published_at') or new.get('document_date') or not raw_path:
        return new
    p = Path(raw_path)
    if not p.is_file() or p.suffix.lower() != '.zip':
        return new
    candidates = []
    with zipfile.ZipFile(p) as z:
        # Only single original page bundles, not arbitrary archives of many articles.
        pages = [n for n in z.namelist() if n.endswith('page.html')]
        if len(pages) != 1 or z.getinfo(pages[0]).file_size > 10_000_000:
            return new
        n = pages[0]
        soup = BeautifulSoup(z.read(n), 'html.parser')
        main = soup.find('main')
        h1 = main.find('h1') if main else None
        if not h1:
            return new
        heading = h1.get_text(' ', strip=True)
        text = main.get_text(' ', strip=True)
        prefix = text.split(heading, 1)[0]
        if len(prefix) > 400 or re.search(r'updated|modified|更新', prefix, re.I):
            return new
        for match in re.finditer(r'\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2}, \d{4}\b', prefix):
            raw = match[0]
            for fmt in ('%b %d, %Y', '%B %d, %Y'):
                try:
                    candidates.append((datetime.strptime(raw, fmt).date().isoformat(), n, raw))
                    break
                except ValueError:
                    pass
    if len(candidates) == 1:
        value, n, raw = candidates[0]
        new.update(published_at=value, date_status='known',
                   date_evidence=f'published_at：原件ZIP内 {n}，main 内首个 h1 之前的文章日期：{raw}',
                   date_note='本地原始HTML文章页头日期；未重新访问网站')
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()
    path = config.LOCAL_DIR / 'knowledge.db'
    conn = sqlite3.connect(f'file:{path.as_posix()}?mode=rw', uri=True)
    rows = conn.execute('''SELECT e.kb_id,e.doc_id,e.topic_version,e.metadata,r.raw_path
        FROM evidence_metadata e JOIN topics t ON e.kb_id=t.kb_id AND e.doc_id=t.id AND e.topic_version=t.version
        LEFT JOIN receipts r ON e.receipt_id=r.id''').fetchall()
    changes, errors = [], []
    for kb, doc, version, payload, raw in rows:
        old = json.loads(payload)
        try:
            new = propose(old, raw)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            errors.append({'doc_id': doc, 'error': str(exc)})
            continue
        if new != old:
            changes.append({'kb_id':kb,'doc_id':doc,'version':version,'old':old,'new':new,'old_payload':payload})
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    folder = config.LOCAL_DIR / 'backups' / ('material-dates-'+stamp)
    folder.mkdir(parents=True)
    report = {'apply':args.apply,'scanned':len(rows),'changes':changes,'errors':errors}
    (folder/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.apply:
        backup = sqlite3.connect(folder/'knowledge-before.db')
        conn.backup(backup)
        backup.close()
        try:
            conn.execute('BEGIN IMMEDIATE')
            for change in changes:
                updated=conn.execute('''UPDATE evidence_metadata SET metadata=? WHERE kb_id=? AND doc_id=? AND topic_version=? AND metadata=?
                    AND EXISTS(SELECT 1 FROM topics t WHERE t.kb_id=evidence_metadata.kb_id AND t.id=evidence_metadata.doc_id AND t.version=evidence_metadata.topic_version)''',
                    (json.dumps(change['new'],ensure_ascii=False),change['kb_id'],change['doc_id'],change['version'],change['old_payload']))
                if updated.rowcount != 1:
                    raise RuntimeError('Source changed concurrently; all metadata edits rolled back')
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    conn.close()
    print(json.dumps({'apply':args.apply,'scanned':len(rows),'changed':len(changes),'errors':len(errors),'report':str(folder/'report.json')},ensure_ascii=False))


if __name__ == '__main__':
    main()
