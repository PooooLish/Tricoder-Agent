"""Freeze the delivery 8/16 experiment locally. Never load config or run a model."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tomllib

ROOT = Path(__file__).resolve().parents[1]
TOP_FILES = {
    'suite.toml', 'README.md', 'experiment-quality-baseline.toml',
    'experiment-quality-memory.toml', 'source-v1-sha256.json', 'source-v2.1-sha256.json',
}


def checked(path: Path, boundary: Path) -> Path:
    """Reject traversal, links/junctions and paths outside the explicit boundary."""
    path = Path(os.path.abspath(path))
    if not path.is_relative_to(boundary):
        raise ValueError('path outside preparation boundary')
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            metadata = part.lstat()
            if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, 'st_file_attributes', 0) & 0x400:
                raise ValueError('linked paths are not supported')
        if part == ROOT:
            break
    return path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare(suite: Path, output: Path) -> Path:
    suite = checked(suite, ROOT)
    output = checked(output, ROOT / 'runtime')
    if output.exists() or output.is_relative_to(suite):
        raise ValueError('output must be a new runtime directory outside the source')
    # Validate the inventory before reading file contents. No config/database copying.
    paths = []
    for parent, dirs, files in os.walk(suite, followlinks=False):
        for name in dirs:
            checked(Path(parent) / name, suite)
        for name in files:
            path = checked(Path(parent) / name, suite)
            relative = path.relative_to(suite)
            parts = relative.parts
            allowed = relative.as_posix() in TOP_FILES or (
                len(parts) >= 3 and parts[0] == 'cases' and (
                    len(parts) == 3 and parts[2] == 'case.toml' or
                    len(parts) >= 4 and parts[2] in {'workspace', 'verifier'} and path.suffix == '.py'
                )
            )
            if not allowed or not stat.S_ISREG(path.stat().st_mode):
                raise ValueError('unexpected suite file')
            paths.append(path)
    data = {p.relative_to(suite).as_posix(): p.read_bytes() for p in sorted(paths)}
    from tricoder.evals.loader import load_experiment
    definition = load_experiment(suite / 'experiment-quality-baseline.toml')
    quality = [c for c in definition.suite.cases if c.execution_kind == 'quality']
    contracts = [c for c in definition.suite.cases if c.execution_kind == 'contract']
    condition = definition.conditions
    if (definition.suite.benchmark_version != 'quality-v2.2' or len(quality) != 19 or len(contracts) != 11
        or any(c.max_rounds != 8 for c in definition.suite.cases)
        or definition.repetitions != 1 or definition.max_trials != 19
        or definition.seed != 23 or definition.time_budget_seconds != 3600
        or len(condition) != 1 or condition[0].provider != 'deepseek'
        or condition[0].model != 'deepseek-v4-flash'
        or condition[0].memory_compaction != 'off' or condition[0].memory_persistence != 'off'):
        raise ValueError('source is not the agreed delivery experiment')
    # One shared, inspectable code snapshot serves both groups. Only source files.
    code_paths = sorted((ROOT / 'src').rglob('*.py')) + [ROOT / 'pyproject.toml', Path(__file__).resolve()]
    code = {checked(p, ROOT).relative_to(ROOT).as_posix(): p.read_bytes() for p in code_paths}
    hashes = {'suite': {p: digest(b) for p, b in data.items()}, 'code': {p: digest(b) for p, b in code.items()}}
    delta = {'variable': 'quality max_rounds', 'baseline': 8, 'candidate': 16,
             'case_changes': [], 'manifest_changes': ['experiment_id', 'suite'],
             'strict_comparison': 'rejected: budget fingerprints differ',
             'real_provider_calls': 0}
    changed = dict(data)
    for case in quality:
        rel = f'cases/{case.id}/case.toml'
        before = data[rel]
        if before.count(b'max_rounds = 8') != 1:
            raise ValueError('source budget declaration is ambiguous')
        after = before.replace(b'max_rounds = 8', b'max_rounds = 16')
        changed[rel] = after
        delta['case_changes'].append({'path': rel, 'before_sha256': digest(before), 'after_sha256': digest(after)})
    manifest = data['experiment-quality-baseline.toml'].decode('utf8')
    parsed = tomllib.loads(manifest)
    if parsed['suite'] != '.' or parsed['experiment_id'] != 'quality-v2-2-baseline':
        raise ValueError('unexpected source manifest')
    # Reserve once, never overwrite a pre-existing output tree.
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError('output already exists') from exc
    def write(relative: str, content: bytes) -> None:
        path = checked(output / relative, output)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            stream.write(content)
    for rel, content in code.items():
        write('code/' + rel, content)
    for budget, files in ((8, data), (16, changed)):
        group = f'budget{budget}'
        for rel, content in files.items():
            write(f'{group}/suite/{rel}', content)
        generated = manifest.replace('suite = "."', 'suite = "suite"').replace(
            'quality-v2-2-baseline', f'delivery-budget{budget}')
        write(f'{group}/experiment.toml', generated.encode('utf8'))
    for name, value in (('source-sha256.json', hashes), ('allowed-delta.json', delta)):
        encoded = (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf8')
        for prefix in ('', 'budget8/', 'budget16/'):
            write(prefix + name, encoded)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--suite', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        prepare(args.suite, args.output)
    except (OSError, ValueError, KeyError):
        parser.exit(2, 'preparation refused: invalid source or output\n')
    print('prepared budget8=19 budget16=19 real_provider_calls=0')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
