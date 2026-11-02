#!/usr/bin/env python3
"""Validate portable records and rebuild their derived indexes (Python 3.10+)."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sys
import tempfile


class Invalid(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def text(value, field):
    require(isinstance(value, str) and bool(value.strip()), f"{field}: expected nonempty text")


def strings(value, field, nonempty=False):
    require(isinstance(value, list), f"{field}: expected list")
    require(not nonempty or bool(value), f"{field}: cannot be empty")
    for item in value:
        text(item, field)


def date(value, field):
    require(isinstance(value, str), f"{field}: expected date")
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise Invalid(f"{field}: expected YYYY-MM-DD") from exc
    require(bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)), f"{field}: expected YYYY-MM-DD")


def seconds(value, field):
    require(value is None or (type(value) in (int, float) and value >= 0 and value < float('inf')),
            f"{field}: expected nonnegative finite seconds or null")


def asset(base, value, field):
    if value is None:
        return
    text(value, field)
    require('\\' not in value and not PureWindowsPath(value).drive and not Path(value).is_absolute()
            and not re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*:', value), f"{field}: use a portable relative path")
    require('..' not in Path(value).parts, f"{field}: parent traversal forbidden")
    target = (base / value).resolve()
    require(target.is_relative_to(base.resolve()), f"{field}: asset escapes record directory")
    require(target.is_file(), f"{field}: missing asset {value}")


def load(path):
    try:
        return json.loads(path.read_text(encoding='utf-8-sig'))
    except (ValueError, OSError) as exc:
        raise Invalid(f"{path.name}: cannot read valid JSON: {exc}") from exc


def validate_record(path, kind):
    d = load(path)
    require(isinstance(d, dict), 'record must be an object')
    require(type(d.get('schema_version')) is int and d['schema_version'] == 1, 'schema_version must be 1')
    for field in ('id', 'title', 'category', 'summary'):
        text(d.get(field), field)
    require(bool(re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', d['id'])), 'invalid ID')
    require(path.parent.name == d['id'], 'ID must match directory name')
    require(d.get('record_type') == kind, 'record_type does not match library')
    modes = d.get('modes')
    strings(modes, 'modes', True)
    require(len(modes) == len(set(modes)) and set(modes) <= {'talk', 'use'}, 'invalid or duplicate modes')
    for field in ('product_name', 'product_form', 'container', 'body_part'):
        require(field in d and (d[field] is None or isinstance(d[field], str) and d[field].strip()),
                f'{field}: use nonempty text or null')
    strings(d.get('tags'), 'tags')
    for field in ('created_on', 'updated_on'):
        date(d.get(field), field)
    require(d['updated_on'] >= d['created_on'], 'updated_on precedes created_on')

    if kind == 'demo':
        p = d.get('provenance')
        require(isinstance(p, dict), 'missing provenance')
        require(p.get('kind') in {'video_analysis', 'legacy_text', 'authored_method'}, 'invalid provenance kind')
        for field in ('description', 'observation_method'):
            text(p.get(field), f'provenance.{field}')
        strings(p.get('limitations'), 'provenance.limitations')
        require(p.get('time_reliability') in {'measured', 'approximate', 'unknown', 'not_applicable'},
                'invalid time_reliability')
        seconds(p.get('duration_seconds'), 'duration_seconds')
        if p.get('analyzed_on') is not None:
            date(p['analyzed_on'], 'analyzed_on')
        if p.get('source_url') is not None:
            require(isinstance(p['source_url'], str) and bool(re.match(r'^https?://[^\s]+$', p['source_url'])),
                    'source_url must be HTTP(S) or null')
        for field in ('source_asset', 'notes_path'):
            asset(path.parent, p.get(field), f'provenance.{field}')
        strings(d.get('preconditions'), 'preconditions', True)
        units = d.get('units')
        require(isinstance(units, list) and bool(units), 'units must be nonempty')
        ids = set()
        for u in units:
            require(isinstance(u, dict), 'unit must be an object')
            for field in ('id', 'name', 'action', 'transition', 'end_state', 'expected_effect', 'prompt_fragment'):
                text(u.get(field), f'unit.{field}')
            require(u['id'] not in ids, 'duplicate unit ID')
            ids.add(u['id'])
            require(u.get('source_time') is None or isinstance(u['source_time'], str), 'invalid source_time')
            strings(u.get('start_conditions'), 'start_conditions', True)
            strings(u.get('avoid'), 'avoid')
        frames = d.get('keyframes')
        require(isinstance(frames, list), 'keyframes must be a list')
        for frame in frames:
            require(isinstance(frame, dict), 'keyframe must be an object')
            text(frame.get('path'), 'keyframe.path')
            asset(path.parent, frame['path'], 'keyframe.path')
            seconds(frame.get('timestamp_seconds'), 'keyframe.timestamp_seconds')
            text(frame.get('role'), 'keyframe.role')
            text(frame.get('observation'), 'keyframe.observation')
    else:
        require(d.get('status') in {'unconfirmed', 'user_confirmed_effective'}, 'invalid experience status')
        context = d.get('context')
        require(isinstance(context, dict), 'missing context')
        for field in ('first_frame_state', 'problem', 'model'):
            text(context.get(field), f'context.{field}')
        require(context.get('model_version') is None or isinstance(context['model_version'], str), 'invalid model_version')
        require(isinstance(context.get('settings'), dict), 'settings must be an object')
        seconds(context.get('duration_seconds'), 'context.duration_seconds')
        strings(d.get('applicability'), 'applicability', True)
        strings(d.get('unknowns'), 'unknowns')
        strings(d.get('related_demo_ids'), 'related_demo_ids')
        strings(d.get('attachments'), 'attachments')
        for item in d['attachments']:
            asset(path.parent, item, 'attachment')
        attempts = d.get('attempts')
        require(isinstance(attempts, list) and bool(attempts), 'attempts must be nonempty')
        by_id = {}
        for a in attempts:
            require(isinstance(a, dict), 'attempt must be an object')
            text(a.get('id'), 'attempt.id')
            require(a['id'] not in by_id, 'duplicate attempt ID')
            by_id[a['id']] = a
            date(a.get('date'), 'attempt.date')
            for field in ('observations', 'hypotheses', 'changes'):
                strings(a.get(field), f'attempt.{field}')
            asset(path.parent, a.get('prompt_path'), 'prompt_path')
            require(a.get('outcome') in {'unverified', 'failed', 'improved', 'succeeded'}, 'invalid outcome')
            feedback = a.get('feedback')
            if feedback is not None:
                require(isinstance(feedback, dict) and feedback.get('author') == 'user', 'feedback must cite user')
                date(feedback.get('date'), 'feedback.date')
                text(feedback.get('text'), 'feedback.text')
                strings(feedback.get('resolved'), 'feedback.resolved')
                strings(feedback.get('remaining'), 'feedback.remaining')
        strings(d.get('confirmed_scope'), 'confirmed_scope')
        if d['status'] == 'user_confirmed_effective':
            key = d.get('confirmed_attempt')
            require(isinstance(key, str) and key in by_id, 'confirmed_attempt must refer to an existing attempt')
            a = by_id[key]
            require(a['outcome'] in {'improved', 'succeeded'} and isinstance(a.get('feedback'), dict)
                    and bool(a['feedback'].get('resolved')), 'confirmation requires user feedback with resolved scope')
            require(bool(d['confirmed_scope']), 'confirmation requires confirmed_scope')
        else:
            require(d.get('confirmed_attempt') is None and not d['confirmed_scope'],
                    'unconfirmed case must clear current confirmation; keep history in attempts')
    return d


def collect(root):
    records = {'demo': [], 'experience': []}
    seen = set()
    for kind in records:
        base = root / f'{kind}-library' / 'entries'
        for path in sorted(base.rglob('record.json')):
            require(path.resolve().is_relative_to(base.resolve()), 'record escapes library')
            require(path.parent.parent == base, f'invalid record depth: {path}')
            try:
                d = validate_record(path, kind)
            except Invalid as exc:
                raise Invalid(f'{path.relative_to(root)}: {exc}') from exc
            require(d['id'] not in seen, f'duplicate record ID: {d["id"]}')
            seen.add(d['id'])
            records[kind].append((path, d))
    demo_ids = {d['id'] for _, d in records['demo']}
    for _, d in records['experience']:
        require(set(d['related_demo_ids']) <= demo_ids, f'{d["id"]}: unknown related demo')
    return records


def index_data(root, kind, records):
    entries = []
    for path, d in records:
        row = {key: d[key] for key in ('id', 'title', 'modes', 'category', 'product_name', 'product_form',
                                      'container', 'body_part', 'tags', 'summary')}
        row['path'] = path.relative_to(root / f'{kind}-library').as_posix()
        row['source_kind' if kind == 'demo' else 'status'] = d['provenance']['kind'] if kind == 'demo' else d['status']
        entries.append(row)
    return {'schema_version': 1, 'library': kind, 'entries': entries}


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.index-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write('\n')
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def check_links(root, rebuilding=False):
    # Instructions link only to real relative files or external web references.
    for path in [root / 'SKILL.md', *sorted((root / 'references').glob('*.md'))]:
        for raw in re.findall(r'\[[^\]]*\]\(([^)]+)\)', path.read_text(encoding='utf-8')):
            target = raw.split('#', 1)[0]
            if not target or target.startswith(('https://', 'http://')):
                continue
            require(not PureWindowsPath(target).drive and not Path(target).is_absolute(), f'absolute instruction link: {raw}')
            resolved = (path.parent / target).resolve()
            if rebuilding and resolved in {root / 'demo-library/index.json', root / 'experience-library/index.json'}:
                continue
            require(resolved.is_relative_to(root.resolve()) and resolved.is_file(), f'broken instruction link: {path.name}: {raw}')


def run(root, command):
    root = root.resolve()
    require((root / 'SKILL.md').is_file(), 'root does not contain SKILL.md')
    records = collect(root)
    check_links(root, rebuilding=command == 'reindex')
    for kind, rows in records.items():
        expected = index_data(root, kind, rows)
        path = root / f'{kind}-library' / 'index.json'
        if command == 'reindex':
            atomic_json(path, expected)
        else:
            require(path.is_file() and load(path) == expected, f'{kind} index missing or stale; run reindex')
    return {kind: len(rows) for kind, rows in records.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['validate', 'reindex'])
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        result = run(args.root, args.command)
        print(json.dumps({'ok': True, 'command': args.command, 'records': result}, ensure_ascii=False))
        return 0
    except (Invalid, OSError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    raise SystemExit(main())
