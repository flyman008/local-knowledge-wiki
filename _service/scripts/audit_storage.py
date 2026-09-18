"""Read-only storage inventory; reports only, never deletes or changes originals."""
import argparse
import hashlib
import json
import zipfile
from pathlib import Path
from collections import defaultdict

MEDIA = {'.png','.jpg','.jpeg','.webp','.gif','.svg','.mp4','.webm','.mov','.mp3','.wav','.m4a','.ogg','.aac'}

def audit(root):
    folders=defaultdict(lambda:{'bytes':0,'files':0})
    originals=defaultdict(list)
    media=defaultdict(list)
    errors=[]
    files=list(root.rglob('*'))
    for path in files:
        if not path.is_file() or path.is_symlink():
            continue
        try:
            rel=path.relative_to(root)
            group='/'.join(rel.parts[:2]) if len(rel.parts)>2 else rel.parts[0]
            folders[group]['bytes']+=path.stat().st_size
            folders[group]['files']+=1
            if rel.parts[:2]!=('content','raw'):
                continue
            with path.open('rb') as stream:
                digest=hashlib.file_digest(stream,'sha256').hexdigest()
            originals[digest].append({'path':str(rel),'bytes':path.stat().st_size})
            if path.suffix.lower() not in {'.zip','.docx','.pptx','.xlsx'}:
                continue
            with zipfile.ZipFile(path) as z:
                for entry in z.infolist():
                    if Path(entry.filename).suffix.lower() not in MEDIA or entry.is_dir():
                        continue
                    if entry.file_size>512*1024*1024:
                        errors.append({'path':str(rel),'entry':entry.filename,'error':'media over 512MB skipped'})
                        continue
                    with z.open(entry) as stream:
                        digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    media[digest].append({'package':str(rel),'entry':entry.filename,'bytes':entry.file_size,'compressed_bytes':entry.compress_size})
        except (OSError,zipfile.BadZipFile,RuntimeError) as exc:
            errors.append({'path':str(path),'error':str(exc)})
    duplicates=[{'sha256':h,'copies':v,'redundant_bytes':sum(x['bytes'] for x in v[1:])} for h,v in originals.items() if len(v)>1]
    repeated=[{'sha256':h,'copies':v,'redundant_uncompressed_bytes':sum(x['bytes'] for x in v[1:]),
               'estimated_redundant_compressed_bytes':sum(x['compressed_bytes'] for x in v[1:])} for h,v in media.items() if len(v)>1]
    return {'root':str(root),'read_only':True,'folders':dict(sorted(folders.items())),
            'summary':{'total_file_bytes':sum(v['bytes'] for v in folders.values()),'raw_files':sum(len(v) for v in originals.values()),
                       'identical_original_groups':len(duplicates),'identical_original_redundant_bytes':sum(v['redundant_bytes'] for v in duplicates),
                       'media_entries':sum(len(v) for v in media.values()),'unique_media':len(media),
                       'media_uncompressed_bytes':sum(x['bytes'] for v in media.values() for x in v),
                       'media_redundant_uncompressed_bytes':sum(v['redundant_uncompressed_bytes'] for v in repeated),
                       'media_estimated_redundant_compressed_bytes':sum(v['estimated_redundant_compressed_bytes'] for v in repeated)},
            'identical_originals':sorted(duplicates,key=lambda x:x['redundant_bytes'],reverse=True),
            'repeated_media':sorted(repeated,key=lambda x:x['redundant_uncompressed_bytes'],reverse=True),
            'notes':['Logical file sizes, not filesystem allocated size.','Media savings are estimates, not additive to identical-original savings.',
                     'Embedded PDF images are not counted. Office and ZIP entries counted; nested ZIPs not traversed.',
                     'No cache is considered disposable without ownership/reference verification. Live writes can change results.'],
            'errors':errors}

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',type=Path,required=True);p.add_argument('--report',type=Path,required=True)
    a=p.parse_args();result=audit(a.root.resolve());a.report.parent.mkdir(parents=True,exist_ok=True)
    a.report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'summary':result['summary'],'folders':result['folders'],'errors':result['errors'],'report':str(a.report)},ensure_ascii=False))
