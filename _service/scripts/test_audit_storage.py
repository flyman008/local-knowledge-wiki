"""Small deterministic inventory fixture; no live data."""
import tempfile
import zipfile
from pathlib import Path
from audit_storage import audit

with tempfile.TemporaryDirectory() as tmp:
    root=Path(tmp);raw=root/'content'/'raw';raw.mkdir(parents=True)
    image=b'same image bytes'
    for name,body in [('a.zip','article A'),('b.zip','article B')]:
        with zipfile.ZipFile(raw/name,'w',zipfile.ZIP_DEFLATED) as z:
            z.writestr('image.png',image);z.writestr('page.html',body)
    (raw/'copy.zip').write_bytes((raw/'a.zip').read_bytes())
    before={p.name:p.read_bytes() for p in raw.iterdir()}
    r=audit(root);s=r['summary']
    assert s['raw_files']==3 and s['identical_original_groups']==1
    assert s['media_entries']==3 and s['unique_media']==1
    assert s['media_redundant_uncompressed_bytes']==2*len(image)
    assert not r['errors'] and before=={p.name:p.read_bytes() for p in raw.iterdir()}
    print('PASS: duplicate packages/media counted separately; originals unchanged')
