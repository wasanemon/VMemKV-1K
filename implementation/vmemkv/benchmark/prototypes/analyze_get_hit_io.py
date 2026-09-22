#!/usr/bin/env python3
"""CLOCK_MONOTONICの単件Get区間にpread/bio/request/待機を対応付ける。"""
from bisect import bisect_left, bisect_right
from collections import Counter
import json
from pathlib import Path
import re
import statistics
import subprocess

ROOT = Path(__file__).resolve().parents[4]
OUT = ROOT / 'build/ltm/results/get-hit-io-20260922'
TRACE = OUT / 'trace'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def summarize(items):
    result = []
    for crossing in [True, False]:
        for mode in ['baseline', 'cross-pread']:
            group = [x for x in items if x['crossing'] == crossing and x['mode'] == mode]
            result.append({'crossing': crossing, 'mode': mode, 'count': len(group),
                'median_latency_us': statistics.median(x['latency_ns'] / 1000 for x in group),
                'distributions': {k: dict(Counter(x[k] for x in group)) for k in [
                    'device_reads', 'device_read_sectors', 'device_merges', 'read_bytes',
                    'major_faults', 'voluntary_switches', 'idle_reads', 'resident_after']}})
    return result


if __name__ == '__main__':
    command = ['perf', 'script', '--ns', '-F', 'trace:comm,pid,tid,time,event,trace',
               '-i', str(TRACE / 'perf.data')]
    with (TRACE / 'perf-script.txt').open('w') as output, (TRACE / 'perf-script.stderr').open('w') as errors:
        subprocess.run(command, stdout=output, stderr=errors, check=True)
    save(TRACE / 'analysis-command.json', command)
    with (TRACE / 'perf-events.txt').open('w') as output:
        subprocess.run(['perf', 'evlist', '-v', '-i', str(TRACE / 'perf.data')], stdout=output, check=True)
    text = (TRACE / 'perf-script.txt').read_text()
    errors = (TRACE / 'perf-script.stderr').read_text()
    assert not re.search(r'\blost\b', text + errors, re.I), 'trace reports lost events'
    pattern = re.compile(r'^\s*(.*?)\s+(\d+)/(\d+)\s+(\d+)\.(\d+):\s+(\S+):\s+(.*)$')
    events = []
    for number, line in enumerate(text.splitlines(), 1):
        match = pattern.match(line)
        assert match, f'unparsed trace line {number}: {line}'
        comm, pid, tid, seconds, fraction, event, detail = match.groups()
        item = {'line': number, 'comm': comm, 'pid': int(pid), 'tid': int(tid),
                'time_ns': int(seconds) * 10**9 + int(fraction.ljust(9, '0')),
                'event': event, 'detail': detail}
        if event.startswith('block:'):
            dev = re.match(r'(\d+),(\d+) ', detail)
            span = re.search(r'(\d+) \+ (\d+)', detail)
            assert dev and span
            item.update(device=','.join(dev.groups()), sector=int(span[1]), sectors=int(span[2]))
        events.append(item)
    assert events == sorted(events, key=lambda x: x['time_ns'])
    times = [e['time_ns'] for e in events]
    extents = []
    for line in (OUT / 'filefrag.txt').read_text().splitlines():
        match = re.match(r'\s*\d+:\s*(\d+)\.\.\s*(\d+):\s*(\d+)\.\.\s*(\d+):', line)
        if match:
            extents.append(tuple(map(int, match.groups())))

    def file_sector(page):
        for low, high, physical, _ in extents:
            if low <= page <= high:
                return (physical + page - low) * 8
        raise AssertionError('file page not allocated')

    pid = int((TRACE / 'pid').read_text())
    observations = []
    for op in rows(TRACE / 'operations.jsonl'):
        window = events[bisect_left(times, op['start_ns']):bisect_right(times, op['end_ns'])]
        select = lambda name: [e for e in window if e['event'] == name]
        bios = select('block:block_bio_queue')
        logical = [e for e in bios if e['device'] == '253,2']
        physical = [e for e in bios if e['device'] == '8,0']
        issues = select('block:block_rq_issue')
        completions = select('block:block_rq_complete')
        enters = select('syscalls:sys_enter_pread64')
        exits = select('syscalls:sys_exit_pread64')
        sleeps = [e for e in select('sched:sched_switch')
                  if f'prev_pid={pid} ' in e['detail'] and 'prev_state=D ' in e['detail']]
        count = 2 if op['crossing'] else 1
        expected = {file_sector(op['offset'] // 4096 + i) + j for i in range(count) for j in range(8)}
        actual = {e['sector'] + j for e in logical for j in range(e['sectors'])}
        assert actual == expected and all(e['tid'] == pid for e in logical + physical)
        spans = lambda xs: [(e['sector'], e['sectors']) for e in xs]
        assert spans(physical) == spans(issues) == spans(completions)
        assert all(e['detail'].endswith('[0]') for e in completions)
        assert all(i['time_ns'] < c['time_ns'] for i, c in zip(issues, completions))
        assert len(sleeps) == op['voluntary_switches']
        assert len(issues) == op['device_reads']
        assert sum(e['sectors'] for e in issues) * 512 == op['read_bytes']
        if op['mode'] == 'cross-pread' and op['crossing']:
            assert len(enters) == len(exits) == 1
            request = re.search(r'count: (0x[0-9a-f]+), pos: (0x[0-9a-f]+)', enters[0]['detail'])
            assert request and int(request[1], 16) == op['hint'] and int(request[2], 16) == op['offset']
            assert int(exits[0]['detail'], 16) == op['hint']
            assert enters[0]['time_ns'] < issues[0]['time_ns'] < completions[-1]['time_ns'] < exits[0]['time_ns']
        else:
            assert not enters and not exits
        op.update(pread_calls=len(enters), bio_sectors=[e['sectors'] for e in logical],
                  request_sectors=[e['sectors'] for e in issues], blocked_switches=len(sleeps),
                  serial_requests=all(c['time_ns'] < i['time_ns'] for c, i in zip(completions, issues[1:])),
                  bio_merges=len(select('block:block_bio_backmerge') + select('block:block_bio_frontmerge')),
                  events=[dict(e, relative_us=(e['time_ns'] - op['start_ns']) / 1000) for e in window])
        observations.append(op)
    save(OUT / 'correlated-operations.json', observations)
    summary = {'stats': summarize(rows(OUT / 'stats/operations.jsonl')),
               'trace': summarize(observations), 'event_counts': dict(Counter(e['event'] for e in events)),
               'validated_operations': len(observations), 'trace_groups': []}
    for crossing in [True, False]:
        for mode in ['baseline', 'cross-pread']:
            group = [x for x in observations if x['crossing'] == crossing and x['mode'] == mode]
            summary['trace_groups'].append({'crossing': crossing, 'mode': mode, 'count': len(group),
                'pread_calls': dict(Counter(x['pread_calls'] for x in group)),
                'bio_sizes': dict(Counter(str(x['bio_sectors']) for x in group)),
                'request_sizes': dict(Counter(str(x['request_sectors']) for x in group)),
                'blocked_switches': dict(Counter(x['blocked_switches'] for x in group)),
                'bio_merges': sum(x['bio_merges'] for x in group),
                'all_multiple_requests_serial': all(x['serial_requests'] for x in group if len(x['request_sectors']) > 1)})
    save(OUT / 'summary.json', summary)
    print(json.dumps(summary['trace_groups'], indent=2))
