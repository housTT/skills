"""Bound a complete client-side benchmark stage and retain failed-run evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from benchmark_stage.evidence import validate_performance, validate_server
from benchmark_stage.roofline import load_roofline
from benchmark_stage.performance import performance_profiles, review_performance, select_accuracy_concurrency
from benchmark_stage.subsets import digest


def command(argv, log, deadline, *, terminate_grace_seconds=10):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('benchmark stage exceeded its wall-clock budget')
    with Path(log).open('w') as stream:
        stream.write(json.dumps(argv) + '\n')
        stream.flush()
        process = subprocess.Popen(argv, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait(timeout=remaining)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            # A terminated group leader does not imply its children have exited.
            grace_deadline = time.monotonic() + terminate_grace_seconds
            while True:
                process.poll()
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                except PermissionError:
                    # macOS can return EPERM briefly while a terminated leader
                    # is being reaped. Keep the bounded cleanup attempt active.
                    pass
                if time.monotonic() >= grace_deadline:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    break
                time.sleep(0.05)
            process.wait()
            raise
    if code:
        raise RuntimeError(f'command exited {code}; inspect {log}')


def run(*, config_path, output):
    config_path = Path(config_path).resolve()
    config = json.loads(config_path.read_text())
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    (output / 'run_config.json').write_text(json.dumps(config, indent=2) + '\n')
    source_manifest = Path(config['manifest'])
    if source_manifest.is_file():
        frozen_manifest = output / 'manifest.json'
        frozen_manifest.write_bytes(source_manifest.read_bytes())
        config['source_manifest'] = str(source_manifest)
        config['manifest'] = str(frozen_manifest)
        (output / 'run_config.json').write_text(json.dumps(config, indent=2) + '\n')
    budget = min(float(config.get('budget_seconds', 3600)), 3600)
    if not 0 < budget <= 3600:
        raise ValueError('budget_seconds must be positive and at most 3600')
    deadline = started + budget
    summary = {'schema_version': 1, 'status': 'running', 'accuracy': {}, 'performance': {}}
    def interrupted(signum, frame):
        raise InterruptedError(f'benchmark interrupted by signal {signum}')
    previous_term = signal.signal(signal.SIGTERM, interrupted)
    try:
        profiles = performance_profiles(config)
        accuracy_cache = config.get('accuracy_prefix_caching', False)
        if type(accuracy_cache) is not bool:
            raise ValueError('accuracy_prefix_caching must be a boolean')
        server_command = config.get('performance_server_command')
        if not isinstance(server_command, list) or not server_command or not all(isinstance(arg, str) for arg in server_command):
            raise ValueError('performance_server_command is required to select and record each configured server profile')
        roofline_command = config.get('roofline_command')
        if not isinstance(roofline_command, list) or not roofline_command or not all(
                isinstance(arg, str) and arg.strip() for arg in roofline_command):
            raise ValueError('roofline_command is required to collect full-phase prefill/decode accounting')

        def phase_accounting(capacity, action):
            command([*roofline_command, '--run-dir', str(output), '--concurrency', str(capacity),
                     '--action', action], output / f'roofline-b{capacity}-{action}.log', deadline)
            if action == 'collect':
                load_roofline(output, required=(str(capacity),))

        def select_server(capacity, baseline=None, *, accuracy=False):
            server_name = 'accuracy-server' if accuracy else f'perf-b{capacity}-server'
            server_path = output / f'{server_name}.json'
            cache_args = ['--enable-prefix-caching'] if accuracy and accuracy_cache else []
            command([*server_command, *cache_args, '--max-num-seqs', str(capacity),
                     '--base-url', config['base_url'], '--output', str(server_path)],
                    output / f'{server_name}.log', deadline)
            server = json.loads(server_path.read_text())
            validate_server(server, capacity, config['model'], config['base_url'],
                            baseline=baseline, output=output, prefix_caching=accuracy and accuracy_cache)
            if not accuracy:
                phase_accounting(capacity, 'check')
            return server

        baseline_server = None
        for concurrency in profiles:
            server = select_server(concurrency, baseline_server)
            if baseline_server is None:
                baseline_server = server
            for warmup in (True, False):
                name = f'perf-b{concurrency}' + ('-warmup' if warmup else '')
                requests = concurrency if warmup else max(8, concurrency * 3)
                argv = [*config.get('benchmark_command', [config.get('vllm_cli', 'vllm'), 'bench', 'serve']), '--backend', 'vllm',
                        '--model', config['model'], '--base-url', config['base_url'],
                        '--endpoint', '/v1/completions', '--dataset-name', 'random',
                        '--random-input-len', '4096', '--random-output-len', str(config.get('output_tokens', 128)),
                        '--random-range-ratio', '0.0', '--num-prompts', str(requests),
                        '--max-concurrency', str(concurrency), '--request-rate', 'inf', '--ignore-eos',
                        '--temperature', '0', '--seed', str(4100 + concurrency + int(warmup)),
                        '--percentile-metrics', 'ttft,tpot,itl,e2el', '--metric-percentiles', '50,95,99',
                        '--save-result', '--save-detailed', '--result-dir', str(output),
                        '--result-filename', f'{name}.json']
                command(argv, output / f'{name}.log', deadline)
                raw = json.loads((output / f'{name}.json').read_text())
                validate_performance(raw, requests, config.get('output_tokens', 128))
                if raw.get('model_id') != config['model'] or raw.get('max_concurrency') != concurrency:
                    raise ValueError(f'{name}: raw performance model or concurrency mismatch')
                if not warmup:
                    summary['performance'][str(concurrency)] = {
                        'concurrency': concurrency, 'requested_input_tokens': 4096,
                        'server_max_num_seqs': server['max_num_seqs'], 'server_identity_sha256': digest(server),
                        'requested_output_tokens': config.get('output_tokens', 128), 'requests': requests,
                        'completed': raw['completed'],
                        **{k: v for k, v in raw.items() if k.startswith(('mean_', 'median_', 'p95_', 'p99_')) or k in
                           ('duration', 'request_throughput', 'output_throughput', 'total_token_throughput',
                            'total_input_tokens', 'total_output_tokens')},
                    }
            # Flush and retain this server's timings before a profile switch can
            # destroy its in-memory counters or task-local logs.
            phase_accounting(concurrency, 'collect')
        load_roofline(output, required=tuple(map(str, profiles)))
        review = review_performance(summary['performance'], config.get('performance_expectations'))
        summary['performance_review'] = review
        (output / 'performance_review.json').write_text(json.dumps(review, indent=2) + '\n')
        if any(not row['within_expectation'] for row in review.values()):
            raise ValueError('serving performance outside expectations; investigate overhead before accuracy')
        accuracy_concurrency, speedups = select_accuracy_concurrency(
            summary['performance'], config.get('accuracy_concurrency'))
        summary['serial_batch1_speedups'] = speedups
        if config['tasks']:
            accuracy_server = select_server(accuracy_concurrency, baseline_server, accuracy=True)
            summary['accuracy_server_identity_sha256'] = digest(accuracy_server)
        execution = config.get('accuracy_execution', 'sequential')
        if execution not in ('sequential', 'shared'):
            raise ValueError('accuracy_execution must be sequential or shared')
        summary['accuracy_execution'] = execution
        summary['accuracy_concurrency'] = accuracy_concurrency
        batches = [config['tasks']] if execution == 'shared' and config['tasks'] else [[task] for task in config['tasks']]
        for batch in batches:
            generation = config.get('generation', {}).get(batch[0], {})
            if any(config.get('generation', {}).get(task, {}) != generation for task in batch):
                raise ValueError('shared accuracy requires identical generation overrides for all groups')
            task_output = output if execution == 'shared' else output / batch[0]
            argv = [sys.executable, '-m', 'benchmark_stage', 'evaluate', '--model', config['model'],
                    '--base-url', config['base_url'], '--manifest', config['manifest'],
                    '--task', ','.join(batch), '--output', str(task_output),
                    '--generation', json.dumps(generation), '--concurrency', str(accuracy_concurrency)]
            if execution == 'shared':
                argv.append('--shared')
            command(argv, output / ('accuracy-shared.log' if execution == 'shared' else f'{batch[0]}.log'), deadline)
            for task in batch:
                result = json.loads((output / task / 'results.json').read_text())
                summary['accuracy'][task] = result['benchmark_stage']
        summary['status'] = 'completed'
    except BaseException as exc:
        summary['status'] = 'failed'
        summary['error'] = f'{type(exc).__name__}: {exc}'
        raise
    finally:
        from benchmark_stage.report import write_report
        try:
            write_report(output, config, summary)
        except Exception as report_error:
            summary['report_error'] = f'{type(report_error).__name__}: {report_error}'
            if summary['status'] == 'completed':
                summary['status'] = 'failed'
                summary['error'] = 'Report generation failed: ' + summary['report_error']
        summary['elapsed_seconds'] = time.monotonic() - started
        if summary['status'] == 'completed' and summary['elapsed_seconds'] >= budget:
            summary['status'] = 'failed'
            summary['error'] = 'Stage including reporting exceeded its configured wall-clock budget'
        report_path = output / 'REPORT.md'
        if summary['status'] == 'failed' and report_path.exists():
            report_path.write_text(report_path.read_text().replace('Status: completed.', 'Status: failed.', 1))
        with (output / 'REPORT.md').open('a') as report:
            if summary['status'] == 'failed':
                report.write(f"\nFinal status: failed. {summary.get('error', 'Benchmark failed')}\n")
            report.write(f"\nTotal client-stage wall time: {summary['elapsed_seconds']:.1f} seconds.\n")
        (output / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
        signal.signal(signal.SIGTERM, previous_term)
    if summary['status'] != 'completed':
        raise RuntimeError(summary.get('error', 'benchmark failed'))
    return summary
