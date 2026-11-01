"""Evidence-first SWOT draft builder for any company. Standard library only; no API calls.

Run:  python swot.py --sentiment output/sentiment_RUN
      python swot.py --sentiment output/sentiment_RUN --financial "gm_analysis.json::General Motors"
      python swot.py --sentiment output/sentiment_RUN --financial ford.json --financial tesla.json

--financial FILE[::Company name]   Ratio-engine analysis JSON. The optional ::Name tells the builder
                                  which company in the sentiment results it belongs to. Without it the
                                  builder tries the JSON's own client name, then tickers and aliases.
Companies with no financial file (private companies, small local offices) are fine: the strengths /
weaknesses sections stay empty and the draft says so.
"""
import argparse
import hashlib
import json
import math
import re
from datetime import datetime, timezone
from pathlib import Path


def load(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def finite(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def norm(s):
    return re.sub(r'[^a-z0-9]+', '', str(s or '').lower())


def company_keys(label, profile):
    """Everything a financial file's name could legitimately match for one sentiment company."""
    keys = {norm(label), norm(str(label).split(' (')[0])}
    if isinstance(profile, dict):
        keys.add(norm(profile.get('name')))
        keys.update(norm(a) for a in profile.get('all_aliases', []) + profile.get('aliases', []))
        keys.update(norm(t) for t in profile.get('tickers', []))
    keys.discard('')
    return keys


def split_financial_arg(text):
    if '::' in text:
        path, name = text.split('::', 1)
        return Path(path), name.strip()
    return Path(text), None


def financial_points(path, data):
    points = {'strengths': [], 'weaknesses': []}
    held = []
    latest = data.get('latest_period')
    notes = data.get('data_notes', [])
    mapping_failed = any('mapping check failed' in str(n).lower() for n in notes)
    for name, m in data.get('metrics', {}).items():
        if not isinstance(m, dict) or m.get('band') not in ('strength', 'weakness'):
            continue
        ref = {'file': str(path), 'file_sha256': digest(path),
               'json_pointer': '/metrics/' + name.replace('~', '~0').replace('/', '~1'),
               'metric': name, 'value': m.get('latest_value'), 'as_of': m.get('as_of'),
               'threshold': m.get('threshold'), 'trend': m.get('trend')}
        why = []
        if not finite(m.get('latest_value')):
            why.append('Missing or non-finite metric')
        if not latest or m.get('as_of') != latest:
            why.append('Metric is not from the latest financial period')
        if mapping_failed and any(w in name for w in ('debt', 'equity', 'interest')):
            why.append('Upstream mapping validation failed; leverage/capital metrics require review')
        point = {'id': 'fin:' + name,
                 'statement': name.replace('_', ' ').capitalize() + ' classified as ' + m['band']
                              + ' by the upstream ratio engine',
                 'value': m.get('latest_value'), 'as_of': m.get('as_of'),
                 'basis': 'upstream_threshold_not_independently_validated', 'evidence': [ref],
                 'human_review_required': True}
        if why:
            held.append({'point': point, 'reasons': why})
        else:
            points['strengths' if m['band'] == 'strength' else 'weaknesses'].append(point)
    return points, held, notes


def news_candidates(label, summary, rows):
    if rows is None:
        pool = summary.get('top_opportunities', []) + summary.get('top_threats', [])
    else:
        pool = [r for r in rows if r.get('company') == label]
    out, excluded, seen = [], [], set()
    for r in pool:
        eid = r.get('event_id')
        if not eid or eid in seen:
            continue
        seen.add(eid)
        why = []
        if r.get('evidence_verified') is not True:
            why.append('Evidence quote did not pass upstream text-match check')
        if not r.get('url') or not r.get('content_hash') or not r.get('evidence'):
            why.append('Incomplete article provenance')
        if r.get('about_company') is False:
            why.append('Model flagged article as not materially about company')
        if r.get('scope_relevance') == 'out_of_scope':
            why.append('Article concerns places outside the chosen scope')
        if r.get('method') not in (None, 'llm'):
            why.append('Keyword score is not eligible for SWOT evidence')
        s = r.get('sentiment')
        if not finite(s) or abs(s) < .25:
            why.append('Neutral or missing sentiment')
        point = {'id': 'news:' + eid, 'event_id': eid, 'title': r.get('title'), 'summary': r.get('summary'),
                 'sentiment': s, 'bucket': r.get('bucket'), 'scope_relevance': r.get('scope_relevance'),
                 'candidate_direction': ('favorable' if finite(s) and s > 0 else 'adverse'),
                 'evidence': [{'url': r.get('url'), 'content_hash': r.get('content_hash'),
                               'quote': r.get('evidence'), 'upstream_quote_match': r.get('evidence_verified'),
                               'source': r.get('source'), 'published_at': r.get('published_at')}],
                 'ranking_weight': r.get('weight'), 'human_review_required': True,
                 'review_reason': 'Sentiment is not SWOT classification. Decide internal/external, '
                                  'materiality, and duplicate-story membership.'}
        if why:
            excluded.append({'event_id': eid, 'reasons': why})
        else:
            out.append(point)
    out.sort(key=lambda x: -(float(x.get('ranking_weight') or 0) * abs(x['sentiment'])))
    return out, excluded


def markdown(report):
    lines = ['# FinAnalyst SWOT draft', '',
             'Human review required. Empty quadrants mean insufficient classified evidence, not absence of risks.', '']
    for company, b in report['companies'].items():
        lines += ['## ' + company, '', 'Scope: ' + str(b['scope']),
                  'Financial period: ' + str(b['financial_period']), '', 'Coverage: ' + json.dumps(b['coverage']), '']
        for quadrant in ('strengths', 'weaknesses', 'opportunities', 'threats'):
            lines += ['### ' + quadrant.capitalize(), '']
            for x in b[quadrant]:
                lines.append('- ' + x['statement'] + ': ' + str(x['value']) + ' (' + str(x['as_of']) + '); ID ' + x['id'])
            if not b[quadrant]:
                lines.append('No approved points yet.')
            lines.append('')
        lines += ['### News candidates awaiting SWOT classification', '']
        for x in b['news_candidates']:
            e = x['evidence'][0]
            lines += ['- ' + str(x['title']) + ' | ' + x['candidate_direction'] + ' | ID ' + x['id'],
                      '  - Model summary: ' + str(x['summary']),
                      '  - Quote: ' + str(e['quote']), '  - Source: ' + str(e['url']),
                      '  - Extracted-text hash: ' + str(e['content_hash'])]
        lines += ['', '### Warnings', ''] + ['- ' + str(w) for w in b['warnings']] + ['']
    return '\n'.join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sentiment', type=Path, required=True, help='Sentiment run folder, or company_sentiment.json')
    ap.add_argument('--financial', action='append', default=[], metavar='FILE[::Company]',
                    help='Ratio-engine analysis JSON; repeat per company; optional ::Company name')
    ap.add_argument('--outdir', type=Path, default=Path('output'))
    args = ap.parse_args()
    sf = args.sentiment / 'company_sentiment.json' if args.sentiment.is_dir() else args.sentiment
    summary = load(sf)
    af = sf.parent / 'article_scores.json'
    articles = load(af).get('items', []) if af.exists() else None
    companies = summary.get('companies', {})

    fin_for = {}                      # sentiment label -> (path, data)
    unmatched = []
    for arg in args.financial:
        path, forced = split_financial_arg(arg)
        data = load(path)
        if not isinstance(data.get('metrics'), dict):
            ap.error(str(path) + ' has no "metrics" object - is it a ratio-engine analysis file?')
        wanted = norm(forced) if forced else norm(data.get('client') or data.get('company'))
        hit = [lab for lab, blk in companies.items() if wanted and wanted in company_keys(lab, blk.get('profile'))]
        if len(hit) == 1 and hit[0] not in fin_for:
            fin_for[hit[0]] = (path, data)
        elif len(hit) > 1:
            ap.error(str(path) + ' matches several companies (' + ', '.join(hit) + '); add ::Exact company label')
        else:
            unmatched.append((path, data, forced))

    report = {'stage': 'swot_draft', 'generated_at': datetime.now(timezone.utc).isoformat(),
              'status': 'requires_human_review', 'sentiment_source': str(sf),
              'sentiment_sha256': digest(sf), 'companies': {}}
    for label, block in companies.items():
        profile = block.get('profile') or {}
        warnings = ['News sentiment is not automatically an external opportunity/threat.',
                    'Duplicate stories may remain across event IDs; no deduplication repair is performed.',
                    'Quote matching checks text presence, not factual truth or support for an inference.',
                    'Selected news is a biased sample; net sentiment is not an investment rating.']
        b = {'scope': profile.get('scope_text', 'not recorded'), 'strengths': [], 'weaknesses': [],
             'opportunities': [], 'threats': [], 'financial_period': None, 'financial_review_queue': [],
             'coverage': block.get('coverage', {}), 'warnings': warnings}
        if label in fin_for:
            path, data = fin_for[label]
            points, held, notes = financial_points(path, data)
            b.update(points)
            b['financial_period'] = data.get('latest_period')
            b['financial_review_queue'] = held
            b['financial_data_notes'] = notes
            warnings.append('Financial thresholds and input mappings require review; financial and news periods differ.')
            if profile.get('scope') == 'local':
                warnings.append('Financial statements are usually for the whole company and may not describe '
                                'the chosen location(s): ' + str(profile.get('scope_text')))
        else:
            warnings.append('No financial analysis provided for this company (it may be private or a local office); '
                            'strengths and weaknesses are intentionally empty.')
        b['news_candidates'], b['excluded_news'] = news_candidates(label, block, articles)
        if articles is None:
            warnings.append('article_scores.json missing: only top-list candidates available.')
        report['companies'][label] = b
        print(label + ': ' + ', '.join(str(len(b[k])) + ' ' + k for k in ('strengths', 'weaknesses', 'news_candidates')))
    for path, data, forced in unmatched:
        print('NOTE: ' + str(path) + ' was not matched to any company in the sentiment results '
              '(use FILE::Company name). Ignored.')
        report.setdefault('unmatched_financial_files', []).append(str(path))
    out = args.outdir / ('swot_' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
    out.mkdir(parents=True, exist_ok=False)
    (out / 'swot.json').write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    (out / 'swot.md').write_text(markdown(report), encoding='utf-8')
    print('Saved ->', out)


if __name__ == '__main__':
    main()
