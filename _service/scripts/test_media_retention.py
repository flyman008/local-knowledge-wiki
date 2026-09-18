"""Isolated HTTP lifecycle and destructive boundaries. No live files/models."""
from test_taxonomy import client, connect, check, cfg
import io
import json
import zipfile
from datetime import timedelta
from pathlib import Path
from PIL import Image
import media_retention as m

TEXT = '图片01：这是业务流程图，保留完整的连线与字段信息。\n00:00—00:05：说话人甲介绍产品，完整转写已核实。\n00:00—00:05：画面先展示门店，再切换到订单，顺序已核实。'
buf=io.BytesIO();Image.new('RGBA',(400,300),(12,20,30,255)).save(buf,format='PNG',compress_level=0)
png=buf.getvalue()

def package(entries):
    b=io.BytesIO()
    with zipfile.ZipFile(b,'w') as z:
        for n,d in entries.items(): z.writestr(zipfile.ZipInfo(n,date_time=(2026,1,1,0,0,0)),d)
    return b.getvalue()

def submit(title,entries,complete=True):
    raw=package(entries)
    r=client.post('/api/archive_full_file',data={'title':title,'extract_text':TEXT,'wiki_text':'说明：'+TEXT,'parse_complete':str(complete).lower()},files={'file':(title+'.zip',raw,'application/zip')})
    check(r.status_code==200)
    rid=r.json()['receipt_id']; inv=client.get('/api/media/inventory',params={'receipt_id':rid}).json()
    manifest={'receipt_id':rid,'raw_sha256':inv['raw_sha256'],'checked_by':'isolated-test',
              'source_url':'https://example.org/source','refetchable':True,'source_checked_at':m.now().isoformat(),
              'items':[{'name':x['name'],'sha256':x['sha256'],'complete':True,'locator':'正文图1','quote':TEXT.splitlines()[0]} for x in inv['items'] if x['kind']!='other']}
    return rid, manifest

def prepare(payload,assets=None):
    return client.post('/api/media/prepare',data={'payload':json.dumps(payload)},files={'file':('frames.zip',assets,'application/zip')} if assets else None)
def status(rid):return client.get('/api/media/status',params={'receipt_id':rid}).json()
def action(rid,a):return client.post('/api/media/action',json={'receipt_id':rid,'action':a})
def expire(rid):
    c=connect();p=m.read(c,rid);p['purge_after']=(m.now()-timedelta(seconds=1)).isoformat();m.save(c,p);c.commit();c.close()
def rawpath(rid):
    c=connect();r=c.execute('SELECT raw_path FROM receipts WHERE id=?',(rid,)).fetchone();c.close();return Path(r['raw_path'])

rid,p=submit('image-ok',{'article.md':b'article','image.png':png})
r=prepare(p);check(r.status_code==200 and r.json()['state']=='ready')
o=r.json()['objects'];check(next(x for x in o if x['name']=='image.png')['encoding']=='webp')
check(rawpath(rid).exists())
check(action(rid,'stage').json()['state']=='staged');check(m.sweep(cfg)==[])
check(action(rid,'cancel').json()['state']=='cancelled');check(rawpath(rid).exists())
check(action(rid,'stage').status_code==200);expire(rid)
check(m.sweep(cfg)[0]['state']=='purged');check(not rawpath(rid).exists())
check(client.get('/api/library/original',params={'receipt_id':rid}).status_code==410)
download=client.get('/api/media/retained',params={'receipt_id':rid})
check(download.status_code==200)
with zipfile.ZipFile(io.BytesIO(download.content)) as z:
    check(z.read('article.md')==b'article')
    check(Image.open(io.BytesIO(z.read('image.png'))).convert('RGBA').tobytes()==Image.open(io.BytesIO(png)).tobytes())
    check('__knowledge_media__/manifest.json' in z.namelist())
check(action(rid,'cancel').status_code==400)

rid,p=submit('local-only',{'a.png':png});p['refetchable']=False
check(prepare(p).json()['state']=='blocked');check(action(rid,'stage').status_code==400);check(rawpath(rid).exists())
rid,p=submit('incomplete',{'a.png':png},False);check(prepare(p).status_code==400)
rid,p=submit('badquote',{'a.png':png});p['items'][0]['quote']='这是一段根本不在解析稿中的伪造引用';check(prepare(p).json()['state']=='blocked')
rid,p=submit('missingitem',{'a.png':png});p['items']=[];check(prepare(p).status_code==400)

# Identical image object across separate packages is stored once.
a,pa=submit('shared-a',{'a.png':png,'a.txt':b'A'})
b,pb=submit('shared-b',{'a.png':png,'b.txt':b'B'})
oa=prepare(pa).json()['objects'];ob=prepare(pb).json()['objects']
check(oa[0]['sha256']==ob[0]['sha256'])
check(action(a,'stage').status_code==200);expire(a)
path=m.object_path(cfg,oa[0]['sha256']);original=path.read_bytes();path.write_bytes(b'corrupt')
check(m.sweep(cfg)[0]['state']=='held');check(rawpath(a).exists());path.write_bytes(original)
check(action(a,'cancel').status_code==200)

# Exact raw shared by two receipts: one approval cannot remove the other's file.
a,pa=submit('same-a',{'a.png':png,'same.txt':b'same'})
b,pb=submit('same-b',{'a.png':png,'same.txt':b'same'})
check(rawpath(a).resolve()==rawpath(b).resolve());check(prepare(pa).status_code==200)
check(action(a,'stage').status_code==200);expire(a)
check(m.sweep(cfg)[0]['state']=='held');check(rawpath(a).exists());action(a,'cancel')

# Video/audio/animation require timed text, checked source and frames.
anim=io.BytesIO();Image.new('RGB',(20,20),'red').save(anim,format='GIF',save_all=True,append_images=[Image.new('RGB',(20,20),'blue')],duration=100,loop=0)
rid,p=submit('dynamic',{'demo.mp4':b'opaque-video','voice.mp3':b'opaque-audio','demo.gif':anim.getvalue(),'article.txt':b'text'})
check(prepare(p).json()['state']=='blocked')
for item in p['items']:
    item.update(source_url='https://example.org/media',refetchable=True,timeline_quote=TEXT.splitlines()[1],timeline_checked=True)
    if item['name']!='voice.mp3':item.update(keyframes=[{'name':'frame.png','seconds':0}],visual_quote=TEXT.splitlines()[2])
r=prepare(p,package({'frame.png':png}));check(r.status_code==200 and r.json()['state']=='ready')
check(set(x['name'] for x in r.json()['objects'])=={'frame.png','article.txt'})
check(action(rid,'stage').status_code==200);expire(rid)
# New unresolved review after staging vetoes purge.
c=connect();e=c.execute('SELECT * FROM evidence_metadata WHERE receipt_id=?',(rid,)).fetchone()
import review
review.add(c,'library',e['doc_id'],e['topic_version'],'uncertain','发现缺失画面',[{'statement':'遗漏','locator':'00:02'}]);c.commit();c.close()
check(m.sweep(cfg)[0]['state']=='held');check(rawpath(rid).exists());action(rid,'cancel')

try:m.unpack(package({'../outside':b'x'}));raise AssertionError('escaped')
except ValueError:pass
c=connect();check(c.execute('SELECT count(*) FROM usage_log').fetchone()[0]==0);c.close()
print('PASS media: retention, pixel identity, dedup, shared references, 7-day grace, cancel, purge, rebuild, corruption/partial/review/path guards; zero models')
