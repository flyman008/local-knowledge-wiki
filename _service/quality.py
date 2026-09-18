"""Deterministic intake checks, not semantic fact verification. No model calls."""
import json
import re
import material_dates


def inspect(wiki, extract, metadata, classification, parse_complete, missing_pages):
    findings = material_dates.findings(metadata)
    def flag(code, message):
        findings.append({'code': code, 'message': message})
    if parse_complete and missing_pages:
        flag('parse_contradiction', '声明解析完整但存在缺失页码')
    if parse_complete and re.search(r'(?:未识别|无法辨认|无法识别|待补图)', extract or ''):
        flag('parse_text_warning', '解析稿含未识别提示；需核对是否与完整性声明矛盾（可能为引用）')
    # Only an explicit classification line, never infer tags from arbitrary prose.
    line = re.search(r'内容标签[：:]([^\n]+)', wiki or '')
    if line:
        declared = re.findall(r'`([^`]+)`', line[1])
        norm = lambda s: {'ai':'AI','saas':'SaaS'}.get(s.lower(), s)
        if declared and {norm(x) for x in declared} != set(classification.get('tags', [])):
            flag('tag_mismatch', '正文显式内容标签与结构化标签不一致')
    if not metadata.get('source_kind') or metadata.get('source_kind') == 'unknown':
        flag('source_kind_missing', '来源类型未知，不能据此判断权威性')
    if not metadata.get('locator'):
        flag('locator_missing', '缺少来源定位；关键结论仍需逐条原文依据')
    claimed = bool(re.search(r'(?:已登记|登记为待核实|已记录为待确认)', wiki or ''))
    if claimed:
        flag('registration_claim', '正文声称已登记：必须逐项核对确认项 ID，不能仅凭存在其他确认项判通过')
    return {'version':'intake-quality-v1', 'findings':findings, 'material_date':material_dates.describe(metadata),
            'registration_claim':claimed, 'content_review':'not_verified',
            'limits':'仅检查结构与有限文字线索，不验证语义、图像覆盖或事实真实性'}


def receipt_report(conn, receipt):
    row = conn.execute('SELECT payload FROM intake_quality WHERE receipt_id=?', (receipt['id'],)).fetchone()
    if not row:
        return {'automatic_check':'not_checked', 'content_review':'not_verified',
                'message':'旧回执或非 prepared 路径没有本轮检查记录，不能视为通过'}
    report = json.loads(row['payload'])
    metadata_row = conn.execute('SELECT metadata FROM evidence_metadata WHERE receipt_id=?', (receipt['id'],)).fetchone()
    metadata = json.loads(metadata_row['metadata']) if metadata_row else {}
    # Date-only metadata repairs are audited separately; report their current state.
    report['findings'] = [f for f in report['findings'] if not f['code'].startswith('source_date_')] + material_dates.findings(metadata)
    report['material_date'] = material_dates.describe(metadata)
    items = receipt.get('review_items', [])
    current = [x for x in items if x.get('topic_version') == receipt.get('topic_version')]
    report['review_ids'] = [x['id'] for x in current]
    report['pending_review_ids'] = [x['id'] for x in current if x['status'] == 'pending']
    report['automatic_check'] = 'attention' if report['findings'] else 'passed'
    report['saved_layers'] = receipt.get('saved_layers')
    report['parse_complete_client_declared'] = receipt.get('parse_complete')
    return report
