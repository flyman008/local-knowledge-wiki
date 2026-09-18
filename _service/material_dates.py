"""Original-source dates, never ingestion/collection/file timestamps."""
import calendar
from datetime import date, datetime, timedelta
import re

FIELDS = ('published_at', 'document_date', 'effective_at')
LABELS = {'published_at': '原文发布于', 'document_date': '材料标注于', 'effective_at': '业务生效于'}


def bounds(value):
    """Preserve year/month precision; reject prose and impossible dates."""
    if not isinstance(value, str):
        return None
    try:
        if re.fullmatch(r'\d{4}', value):
            return date(int(value), 1, 1), date(int(value), 12, 31)
        if re.fullmatch(r'\d{4}-\d{2}', value):
            y, m = map(int, value.split('-'))
            return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            d = date.fromisoformat(value)
            return d, d
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}.*', value):
            d = datetime.fromisoformat(value.replace('Z', '+00:00')).date()
            return d, d
    except (ValueError, OverflowError):
        pass
    return None


def describe(metadata, today=None):
    today = today or date.today()
    dates = {k: metadata[k] for k in FIELDS if bounds(metadata.get(k))}
    invalid = [k for k in FIELDS if metadata.get(k) and k not in dates]
    # Effective date alone does not establish the material's age.
    field = next((k for k in ('published_at', 'document_date') if k in dates), None)
    span = bounds(dates[field]) if field else None
    status = 'known' if field else ('unknown' if metadata.get('date_status') == 'unknown' else 'unrecorded')
    return {'status': status, 'field': field, 'label': LABELS.get(field, '原材料日期未知'),
            'value': dates.get(field), 'dates': dates, 'invalid_fields': invalid,
            'earliest': span[0].isoformat() if span else None,
            'latest': span[1].isoformat() if span else None,
            'older_than_year': bool(span and span[1] < today - timedelta(days=365)),
            'future': bool(span and span[0] > today),
            'note': metadata.get('date_note', ''),
            'evidence': metadata.get('date_evidence', ''),
            'limits': '日期由来源记录提供；较旧不等于失效，较新不等于正确。'}


def findings(metadata):
    d = describe(metadata)
    result = []
    def flag(code, message):
        result.append({'code': code, 'message': message})
    if d['invalid_fields']:
        flag('source_date_format', '材料日期格式无效：使用 YYYY / YYYY-MM / YYYY-MM-DD 或 ISO 时间；未知说明放 date_note')
    if metadata.get('date_status') not in (None, 'known', 'unknown'):
        flag('source_date_status', 'date_status 只支持 known/unknown')
    if d['status'] == 'unrecorded':
        flag('source_date_unrecorded', '未记录原材料日期；请查原文，确实找不到则 date_status=unknown 并说明检查范围')
    if metadata.get('date_status') == 'unknown' and (d['value'] or not metadata.get('date_note')):
        flag('source_date_contradiction', '日期未知与已填日期冲突，或缺少未知原因')
    if metadata.get('date_status') == 'known' and not d['value']:
        flag('source_date_contradiction', '声明日期已知但没有有效发布/材料日期')
    if d['dates'] and not metadata.get('date_evidence'):
        flag('source_date_evidence', '已填日期但缺少 date_evidence（字段、原文位置和日期原话）')
    return result
