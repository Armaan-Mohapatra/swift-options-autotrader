"""Read-only compatibility gate. Never imports or executes Swift trade code."""
import argparse
import ast
import hashlib
import json
import os
import re
import stat
from pathlib import Path

EXPECTED = '0c090e034ba7a761a1eb1a0cd08e06c91ddf8098'
REQUIRED = ('swift_reviewed.py', 'broker_evidence.py', 'journal_migration.py',
            'telegram_notifier.py', 'conftest.py', 'test_swift_reviewed.py',
            'test_broker_evidence.py', 'test_operational_hardening.py',
            'test_portfolio_production.py', 'test_review_findings.py',
            'test_integration_repairs.py')


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def safe(path):
    p = Path(path).absolute()
    for part in (p, *p.parents):
        s = part.lstat()
        if stat.S_ISLNK(s.st_mode) or getattr(s, 'st_file_attributes', 0) & 1024:
            raise ValueError('redirected path')
    return p


def read(path):
    p = safe(path)
    if not p.is_file():
        raise ValueError('expected regular file')
    return p.read_bytes()


def obj(path):
    value = json.loads(read(path).decode('utf-8-sig'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError('expected object')
    return value


def symbols(tree):
    names = set()
    for n in tree.body:
        if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(n.name)
        elif isinstance(n, (ast.Assign, ast.AnnAssign)):
            targets = n.targets if isinstance(n, ast.Assign) else [n.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            names.update(x.asname or x.name.split('.')[0] for x in n.names)
    return names


def verify(root, candidate, expected=EXPECTED):
    root, candidate = safe(root), safe(candidate)
    if not re.fullmatch('[0-9a-f]{40}', expected):
        raise ValueError('invalid expected release')
    if root == candidate or root in candidate.parents or candidate in root.parents:
        raise ValueError('candidate and install must be separate')
    marker = obj(root / 'deployment.json')
    if (marker.get('product') != 'swift-options-autotrader' or marker.get('mode') != 'PAPER_ONLY'
            or marker.get('live_enabled') is not False or marker.get('broker_writes_enabled') is not False):
        raise ValueError('unsafe install marker')
    if read(root / 'active-release.txt').decode('utf-8-sig').strip() != expected:
        raise ValueError('unexpected active release')
    src = safe(root / 'releases' / expected / 'src' / 'swift_autotrader')
    manifest = obj(candidate / 'SHA256SUMS.json')
    if not set(REQUIRED) <= set(manifest):
        raise ValueError('missing required manifest members')
    actual = {p.name for p in candidate.iterdir()}
    if actual != set(manifest) | {'SHA256SUMS.json'}:
        raise ValueError('unexpected or missing candidate file')
    trees = {}
    for name, digest in sorted(manifest.items()):
        if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9_.-]+', name) or name in ('.', '..', 'SHA256SUMS.json'):
            raise ValueError('unsafe manifest name')
        if not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest):
            raise ValueError('invalid manifest digest')
        data = read(candidate / name)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('candidate checksum mismatch: ' + name)
        if name.endswith('.py'):
            trees[name] = ast.parse(data, filename=name, feature_version=(3, 11))
    missing, fingerprints = [], {}
    for name in ('swift_reviewed.py', 'broker_evidence.py', 'journal_migration.py', 'telegram_notifier.py'):
        for node in ast.walk(trees[name]):
            if not isinstance(node, ast.ImportFrom) or not (node.module or '').startswith('swift_autotrader.'):
                continue
            rel = '/'.join(node.module.split('.')[1:])
            path = src / (rel + '.py')
            if not path.exists():
                path = src / rel / '__init__.py'
            try:
                data = read(path)
            except (OSError, ValueError):
                missing.append(node.module + ' (missing module)')
                continue
            fingerprints[str(path.relative_to(src)).replace(os.sep, '/')] = hashlib.sha256(data).hexdigest()
            available = symbols(ast.parse(data, filename=rel, feature_version=(3, 11)))
            missing.extend(node.module + ':' + alias.name for alias in node.names if alias.name not in available)
    return {'status': 'STATIC_COMPATIBILITY_PASS' if not missing else 'STATIC_COMPATIBILITY_BLOCKED',
            'release_marker_matches': True, 'release_bytes_certified': False,
            'candidate_manifest_entries': len(manifest), 'python_files_parsed': len(trees),
            'missing_static_imports': sorted(set(missing)), 'runtime_source_sha256': fingerprints,
            'tests_executed': False, 'broker_calls': False, 'live_certified': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--expected-release', default=EXPECTED)
    args = parser.parse_args()
    try:
        result = verify(args.root, args.candidate, args.expected_release)
    except (OSError, ValueError, SyntaxError) as exc:
        print(json.dumps({'status': 'BLOCKED', 'reason': type(exc).__name__, 'live_certified': False}))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result['status'] == 'STATIC_COMPATIBILITY_PASS' else 2


if __name__ == '__main__':
    raise SystemExit(main())
