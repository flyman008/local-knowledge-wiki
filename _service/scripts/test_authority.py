"""Isolated provenance roundtrip and answer-policy wiring; no real model calls."""
import sys, runpy, json
from pathlib import Path
sys.dont_write_bytecode = True
n = runpy.run_path(str(Path(__file__).with_name("test_taxonomy.py")))
import archive_full, ingest, search, authority, config
cfg = n["cfg"]
metadata = {"source_kind":"repo_wiki", "product":"WOS", "product_version":"test-v1",
            "repository":"test-repo", "commit":"test-commit", "scope":"test-only", "locator":"p.2"}
r = archive_full.archive_full(cfg, kb_id="library", title="authoritytoken",
    raw_data=b"raw-authority",raw_filename="evidence.txt",extract_text="test",wiki_text="authoritytoken",
    source_metadata=metadata)
assert ingest.get_receipt(cfg,r.receipt_id)["source_metadata"] == metadata
hits = search.search(cfg,"authoritytoken","library")
assert hits[0]["source_metadata"] == metadata
assert search.search(cfg,"sharedtoken","library")[0]["source_metadata"]["source_kind"] == "unknown"
class FakeLLM:
    def __init__(self,cfg): pass
    def complete(self,messages,system=None,**kw):
        assert "source-policy-v1" in system and "不能仅凭类型裁决" in system
        assert "test-commit" in messages[0]["content"]
        class R:
            text="fake answer; only wiring verified";model="fake";tokens_in=1;tokens_out=1;elapsed_ms=1
        return R()
search.llm_mod.LLM=FakeLLM
assert search.answer(cfg,"authoritytoken","library")["source_policy"] == "source-policy-v1"
config.load_config=lambda:cfg
import app
from fastapi.testclient import TestClient
c=TestClient(app.app)
response=c.post('/api/archive_full_file',data={"kb_id":"library","title":"api-source",
    "extract_text":"extract","wiki_text":"wiki","source_metadata":json.dumps(metadata)},
    files={"file":("a.txt",b"api raw","text/plain")})
assert response.status_code==200,response.text
assert c.get('/api/receipt/'+response.json()['receipt_id']).json()['source_metadata']==metadata
bad=c.post('/api/archive_full',json={"title":"bad","source_metadata":{"source_kind":"invented"}})
assert bad.status_code==400
print('PASS metadata roundtrip, legacy unknown, HTTP validation, mocked answer policy wiring')
