#!/usr/bin/env python3
"""Drive the external workspace HTML prototype through an owned agent-browser session."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

REPO = Path(__file__).resolve().parents[3]
SCRIPT = Path(__file__).resolve()
REQUIRED_IDS = ('mainSearch', 'panelSearch', 'draftNote', 'submitDraft', 'handoffButton', 'resetButton')
DOM_VIEW = """({title:document.title,url:location.href,theme:document.documentElement.dataset.theme,
viewport:{width:innerWidth,scrollWidth:document.documentElement.scrollWidth},
text:document.body.innerText,note:document.querySelector('#draftNote')?.value,
activeSession:document.querySelector('#sessionChips .active')?.dataset.session,
references:[...document.querySelectorAll('#draftFragments .fragment-card')].map(e=>e.innerText),
messages:[...document.querySelectorAll('#timeline .message')].map(e=>({id:e.id,
author:e.querySelector('.message-header strong')?.innerText,text:e.querySelector('.message-body')?.innerText})),
history:[...document.querySelectorAll('#handoffHistory .history-row')].map(e=>e.innerText)})"""


class Blocked(RuntimeError):
    pass


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_sha(path):
    try:
        return sha(path)
    except OSError:
        return None


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def artifact_check(path):
    if not path.is_file():
        raise Blocked(f'Prototype is not a readable file: {path}')
    try:
        source = path.read_text(encoding='utf-8')
    except UnicodeDecodeError as exc:
        raise Blocked(f'Prototype is not valid UTF-8 HTML: {path}') from exc
    missing = [name for name in REQUIRED_IDS if not re.search(rf'\bid\s*=\s*["\']{re.escape(name)}["\']', source)]
    if '<html' not in source.lower() or missing:
        raise Blocked(f'Artifact is not the supported workspace prototype; missing ids: {missing}')
    binary = shutil.which('agent-browser')
    if not binary:
        raise Blocked('Missing dependency: agent-browser. No installation was attempted.')
    return binary


def safe_run_dir(raw):
    if raw is not None and not raw.strip():
        raise Blocked('Run directory argument must not be empty.')
    path = Path(raw).expanduser().resolve() if raw is not None else (
        Path.home() / 'Projects/_artifacts/codex-agy-sync' /
        f'verify-ui-{time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())}-{uuid.uuid4().hex[:8]}')
    if any(path == root or root in path.parents for root in (Path('/tmp'), Path('/private/tmp'))):
        raise Blocked('Evidence must be outside /tmp and /private/tmp.')
    if path.exists():
        raise Blocked(f'Refusing to overwrite existing run directory: {path}')
    path.mkdir(parents=True)
    return path


class Run:
    def __init__(self, prototype, root):
        self.prototype, self.root = prototype, root
        self.evidence = root / 'evidence'
        self.evidence.mkdir()
        for folder in ('commands', 'screenshots', 'views'):
            (self.evidence / folder).mkdir()
        self.session = 'sync-ui-' + uuid.uuid4().hex
        self.env = {k: v for k, v in os.environ.items() if not k.startswith('AGENT_BROWSER_')}
        self.count, self.current, self.browser_started = 0, None, False
        self.binary = None
        self.report = {
            'surface': 'external HTML prototype simulation', 'prototype': {'path': str(prototype)},
            'repo': {}, 'tool_versions': {}, 'scenarios': [], 'network_observation': {},
            'cleanup': {}, 'result': 'NOT_RUN', 'native_desktop': 'NOT_RUN',
            'real_message_delivery': 'NOT_RUN', 'visual_semantics': 'REVIEW_REQUIRED',
            'session': self.session,
        }
        self.script_sha = sha(SCRIPT)

    def command(self, argv, allow_failure=False):
        self.count += 1
        receipt = {'argv': argv, 'started_at': time.time(), 'exit_code': None, 'stdout': '', 'stderr': ''}
        path = self.evidence / 'commands' / f'{self.count:04d}.json'
        try:
            result = subprocess.run(argv, cwd=REPO, env=self.env, text=True,
                                    capture_output=True, timeout=35)
            receipt.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
        except subprocess.TimeoutExpired as exc:
            receipt.update(exit_code=None, stdout=str(exc.stdout or ''), stderr=str(exc.stderr or ''),
                           error='command timed out after 35 seconds')
        finally:
            receipt['finished_at'] = time.time()
            write_json(path, receipt)
            if self.current:
                self.current['steps'].append({'command': str(path.relative_to(self.root)), **receipt})
        if receipt['exit_code'] != 0 and not allow_failure:
            raise RuntimeError(f'Command failed at {path.name}: {argv}; {receipt.get("stderr")}')
        return receipt

    def browser(self, *args):
        return self.command([self.binary, '--session', self.session, '--idle-timeout', '120000', *args])

    def read(self, expression):
        receipt = self.browser('eval', expression, '--json')
        payload = json.loads(receipt['stdout'])
        if not payload.get('success'):
            raise RuntimeError(f'DOM read failed: {payload}')
        return payload['data']['result']

    def check(self, expected, expression, value=True):
        actual = self.read(expression)
        self.current['expected'].append(expected)
        self.current['actual'].append({'claim': expected, 'value': actual})
        if actual != value:
            raise AssertionError(f'{self.current["id"]}: {expected}; actual={actual!r}')
        return actual

    def start(self, name):
        self.current = {'id': name, 'steps': [], 'expected': [], 'actual': [],
                        'status': 'RUNNING', 'evidence': []}
        self.report['scenarios'].append(self.current)

    def capture(self, suffix=''):
        name = self.current['id'] + suffix
        paths = [self.evidence / 'views' / f'{name}.json',
                 self.evidence / 'views' / f'{name}.snapshot.txt',
                 self.evidence / 'screenshots' / f'{name}.png']
        write_json(paths[0], self.read(DOM_VIEW))
        paths[1].write_text(self.browser('snapshot')['stdout'])
        self.browser('screenshot', str(paths[2]))
        self.current['evidence'].extend(str(p.relative_to(self.root)) for p in paths)

    def finish(self):
        self.capture()
        self.current['status'] = 'PASS'
        self.current = None

    def click(self, selector):
        self.browser('scrollintoview', selector)
        self.browser('click', selector)

    def fill(self, selector, text):
        self.browser('fill', selector, text)

    def reset(self):
        self.click('#resetButton')

    def wait_sent(self):
        self.browser('wait', '--fn', "!document.querySelector('#submitDraft').disabled && !document.querySelector('#draftResponse').classList.contains('hidden')")

    def scroll_reader_top(self):
        height = self.read("document.querySelector('#readerScroll').scrollHeight")
        self.browser('scroll', 'up', str(height), '--selector', '#readerScroll')

    def prepare(self):
        self.binary = artifact_check(self.prototype)
        self.report['prototype']['sha256_before'] = sha(self.prototype)
        for name, argv in [('head', ['git', 'rev-parse', 'HEAD']),
                           ('status', ['git', 'status', '--porcelain=v1']),
                           ('diff', ['git', 'diff', '--binary', 'HEAD']),
                           ('untracked', ['git', 'ls-files', '--others', '--exclude-standard'])]:
            receipt = self.command(argv)
            path = self.evidence / f'git-{name}.txt'
            path.write_text(receipt['stdout'])
            self.report['repo'][name] = receipt['stdout'] if name != 'diff' else str(path.relative_to(self.root))
        self.report['tool_versions'] = {'python': sys.version, 'platform': platform.platform(),
                                        'agent_browser': self.command([self.binary, '--version'])['stdout']}
        self.report['harness_sha256_before'] = self.script_sha
        write_json(self.evidence / 'env-receipt.json', self.report)
        self.start('01-page-readiness')
        try:
            self.browser('open', self.prototype.as_uri())
            self.browser_started = True
            self.browser('set', 'viewport', '1440', '1000')
            self.browser('set', 'media', 'light', 'reduced-motion')
            self.check('Page is usable with Atlas and Orbit projects and initial Codex conversation',
                       "document.readyState==='complete' && document.querySelectorAll('[data-project]').length===2 && document.querySelector('#sessionChips .active')?.dataset.session==='cx-login' && document.querySelector('#workTitle').innerText==='修复登录失败'")
        except Exception as exc:
            raise Blocked(f'Prototype page is not usable: {exc}') from exc
        self.finish()

    def search(self):
        self.start('02-main-search-and-filters')
        self.fill('#mainSearch', 'token')
        self.check('Token search returns history from Codex and AGY',
                   "document.querySelector('#app').classList.contains('searching') && document.querySelectorAll('#workItems [data-result]').length>0 && [...document.querySelectorAll('#workItems .host-mark')].map(e=>e.innerText).includes('Codex') && [...document.querySelectorAll('#workItems .host-mark')].map(e=>e.innerText).includes('AGY')")
        for host in ('Codex', 'AGY'):
            self.click(f'[data-filter="{host}"]')
            self.check(f'{host} filter returns only that source with nonempty results',
                       f"document.querySelectorAll('#workItems .host-mark').length>0 && [...document.querySelectorAll('#workItems .host-mark')].every(e=>e.innerText==={json.dumps(host)})")
        self.fill('#mainSearch', 'no-result-ui-acceptance')
        self.check('Unmatched search visibly shows empty result',
                   "document.querySelector('#workItems').innerText.includes('没有找到相关历史') && document.querySelectorAll('#workItems [data-result]').length===0")
        self.capture('-empty')
        self.fill('#mainSearch', 'token')
        self.click('[data-filter="all"]')
        self.finish()

    def locate(self):
        self.start('03-history-location-and-references')
        self.click('[data-result="login-analysis"]')
        self.browser('wait', '--fn', "!document.querySelector('#sourceBanner').classList.contains('hidden') && document.querySelector('#message-login-analysis').classList.contains('highlight')")
        time.sleep(0.06)
        self.check('History result opens original message and highlights it',
                   "!document.querySelector('#sourceBanner').classList.contains('hidden') && document.querySelector('#message-login-analysis').classList.contains('highlight')")
        self.click('#message-login-analysis [data-add-fragment]')
        self.check('Reference includes its Codex origin and source message',
                   "document.querySelector('#fragmentCount').innerText==='1 段' && document.querySelector('#draftFragments').innerText.includes('auth/client.ts') && document.querySelector('#draftFragments').innerText.includes('Codex')")
        self.click('[data-remove-fragment="login-analysis"]')
        self.check('Removing a reference leaves zero references', "document.querySelector('#fragmentCount').innerText==='0 段'")
        self.click('#message-login-analysis [data-add-fragment]')
        self.check('Re-adding reference restores one Codex reference', "document.querySelector('#fragmentCount').innerText==='1 段'")
        self.finish()

    def shared(self):
        self.start('04-shared-draft-and-keyboard')
        self.fill('#draftNote', '共享草稿 main 到 panel')
        self.browser('press', 'Meta+k')
        self.browser('wait', '--fn', "!document.querySelector('#panelBackdrop').classList.contains('hidden') && document.activeElement?.id==='panelSearch'")
        self.check('Cmd+K opens panel with same note and reference',
                   "!document.querySelector('#panelBackdrop').classList.contains('hidden') && document.querySelector('#panelDraftHost #draftNote').value==='共享草稿 main 到 panel' && document.querySelector('#fragmentCount').innerText==='1 段'")
        self.fill('#draftNote', '共享草稿 panel 返回 main')
        self.capture('-panel')
        self.browser('press', 'Escape')
        self.browser('wait', '--fn', "document.querySelector('#panelBackdrop').classList.contains('hidden') && !!document.querySelector('#mainDraftHost #draftNote')")
        time.sleep(0.06)
        self.check('Escape returns to main and preserves edited draft and reference',
                   "document.querySelector('#panelBackdrop').classList.contains('hidden') && document.querySelector('#mainDraftHost #draftNote').value==='共享草稿 panel 返回 main' && document.querySelector('#fragmentCount').innerText==='1 段'")
        self.finish()

    def panel_search(self):
        self.start('05-panel-search-reference')
        self.reset()
        self.click('#panelViewButton')
        self.browser('wait', '--fn', "!document.querySelector('#panelBackdrop').classList.contains('hidden') && document.activeElement?.id==='panelSearch'")
        self.fill('#panelSearch', 'token')
        self.check('Panel search exposes source preview and reference actions',
                   "document.querySelectorAll('#paletteBody [data-panel-preview]').length>0 && document.querySelectorAll('#paletteBody [data-add-fragment]').length>0")
        self.click('#paletteBody [data-add-fragment="review-note"]')
        self.check('Adding panel result opens shared draft with AGY source text',
                   "!document.querySelector('#panelDraftHost').classList.contains('hidden') && document.querySelector('#draftFragments').innerText.includes('refresh 失败后应清空') && document.querySelector('#fragmentCount').innerText==='1 段'")
        self.finish()

    def roundtrip(self):
        self.start('06-simulated-roundtrip-and-pending')
        self.browser('press', 'Escape')
        self.reset()
        self.click('#message-login-analysis [data-add-fragment]')
        self.fill('#draftNote', '验收问题：复核共享刷新与失败清理')
        self.browser('select', '#targetSession', 'ag-review')
        self.click('#submitDraft')
        self.check('Pending simulation disables send, note, target and mode controls',
                   "['submitDraft','draftNote','targetSession','askModeButton','handoffModeButton','closeDrawer','askButton','handoffButton'].every(id=>document.getElementById(id).disabled) && document.querySelector('#submitDraft').innerText.includes('正在模拟发送')")
        self.wait_sent()
        self.check('Simulation explicitly reports no real host connection',
                   "document.querySelector('#draftResponse').innerText.includes('未连接真实宿主')")
        self.capture('-sent')
        self.click('#closeDrawer')
        self.click('[data-session="ag-review"]')
        self.check('Target AGY conversation contains question, quoted source, and reply',
                   "document.querySelector('#timeline').innerText.includes('验收问题：复核共享刷新与失败清理') && document.querySelector('#timeline').innerText.includes('[引用 Codex') && document.querySelector('#timeline').innerText.includes('建议补齐两个细节')")
        self.capture('-target')
        self.click('[data-session="cx-login"]')
        self.click('#timeline [data-bring-back]')
        self.check('Bring back prepares AGY reply reference for original Codex session',
                   "document.querySelector('#targetSession').value==='cx-login' && document.querySelector('#draftSourceText').innerText.includes('AGY') && document.querySelector('#draftFragments').innerText.includes('建议补齐两个细节')")
        self.click('#submitDraft')
        self.wait_sent()
        self.click('#closeDrawer')
        self.check('Codex conversation receives returned review and its simulated reply',
                   "document.querySelector('#timeline').innerText.includes('这是另一端对方案的复核意见') && document.querySelector('#timeline').innerText.includes('下一步验证并发 401 只触发一次刷新')")
        self.finish()

    def handoff(self):
        self.start('07-complete-handoff-and-deduplication')
        self.reset()
        self.click('#message-login-tool summary')
        before = self.read("[...document.querySelectorAll('#timeline .message')].map(e=>({author:e.querySelector('.message-header strong').innerText,text:e.querySelector('.message-body').textContent.replace(/\\s+/g,' ').trim()}))")
        self.click('#handoffButton')
        self.check('Handoff previews all four completed records including tool history',
                   "document.querySelector('#historyCount').innerText==='4 条 · 全部已完成' && document.querySelectorAll('#handoffHistory .history-row').length===4 && document.querySelector('#handoffHistory').innerText.includes('工具记录')")
        self.capture('-preview')
        self.click('#submitDraft')
        self.wait_sent()
        self.click('#draftResponse [data-open-handoff]')
        target = self.read("document.querySelector('#sessionChips .active').dataset.session")
        self.check('New target is an inherited AGY conversation with preserved authors and message count',
                   "document.querySelector('#sessionChips .active').innerText.includes('AGY') && document.querySelector('#sessionChips .active').innerText.includes('接续会话') && document.querySelectorAll('#timeline .message').length===4")
        self.click('#timeline .tool-details summary')
        copied = self.read("[...document.querySelectorAll('#timeline .message')].map(e=>({author:e.querySelector('.message-header strong').innerText,text:e.querySelector('.message-body').textContent.replace(/\\s+/g,' ').trim()}))")
        self.current['expected'].append('Every inherited message preserves the original author and full text, including expanded tool output')
        self.current['actual'].append({'original_history': before, 'inherited_history': copied})
        if copied != before:
            raise AssertionError('Inherited message author or full text differs from source')
        self.check('Original Codex source session still exists', "!!document.querySelector('[data-session=\"cx-login\"]')")
        self.scroll_reader_top()
        self.click('[data-session="cx-login"]')
        self.click('#handoffButton')
        self.check('Identical source history offers existing handoff instead of new creation',
                   "document.querySelector('#submitDraft').innerText.includes('打开已创建的接续会话')")
        self.click('#submitDraft')
        self.check('Repeat opens same inherited session and keeps exactly three session chips',
                   f"document.querySelector('#sessionChips .active').dataset.session==={json.dumps(target)} && document.querySelectorAll('#sessionChips [data-session]').length===3")
        self.finish()
        return target

    def changed_history(self, old_target):
        self.start('08-new-history-creates-new-handoff')
        self.scroll_reader_top()
        self.click('[data-session="cx-login"]')
        self.click('#askButton')
        self.fill('#draftNote', '新增历史用于再次接续')
        self.browser('select', '#targetSession', 'ag-review')
        self.click('#submitDraft')
        self.wait_sent()
        self.click('#closeDrawer')
        self.click('#handoffButton')
        self.check('New completed history permits a fresh handoff with expanded preview',
                   "document.querySelector('#submitDraft').innerText.includes('模拟创建并接续') && document.querySelectorAll('#handoffHistory .history-row').length===5 && document.querySelector('#handoffHistory').innerText.includes('新增历史用于再次接续')")
        self.click('#submitDraft')
        self.wait_sent()
        self.click('#draftResponse [data-open-handoff]')
        self.check('New history creates distinct inherited session and retains older one',
                   f"document.querySelector('#sessionChips .active').dataset.session!=={json.dumps(old_target)} && document.querySelectorAll('#sessionChips [data-session]').length===4 && !!document.querySelector('[data-session=\"{old_target}\"]') && document.querySelectorAll('#timeline .message').length===5")
        self.finish()

    def reset_check(self):
        self.start('09-reset-restores-initial-state')
        self.reset()
        self.check('Reset restores login source, initial messages, no deliveries or handoffs and closed draft',
                   "document.querySelector('#sessionChips .active').dataset.session==='cx-login' && document.querySelectorAll('#sessionChips [data-session]').length===2 && document.querySelectorAll('#timeline .message').length===4 && document.querySelectorAll('#timeline .delivery').length===0 && document.querySelector('#drawer').classList.contains('hidden') && document.querySelector('#mainSearch').value==='' && document.querySelector('#fragmentCount').innerText==='0 段'")
        self.finish()

    def responsive(self):
        self.start('10-theme-and-responsive-layout')
        for theme in ('light', 'dark'):
            if self.read('document.documentElement.dataset.theme') != theme:
                self.click('#themeButton')
            self.check(f'{theme} theme is applied', 'document.documentElement.dataset.theme', theme)
            for width in (390, 780, 1440):
                self.browser('set', 'viewport', str(width), '1000')
                self.check(f'{theme} at {width}px has no document horizontal overflow',
                           f"innerWidth==={width} && document.documentElement.scrollWidth<=innerWidth")
                self.capture(f'-{theme}-{width}-main')
                self.click('#panelViewButton')
                self.check(f'{theme} panel at {width}px has no document horizontal overflow',
                           "document.documentElement.scrollWidth<=innerWidth && !document.querySelector('#panelBackdrop').classList.contains('hidden')")
                self.capture(f'-{theme}-{width}-panel')
                self.browser('press', 'Escape')
        self.finish()

    def observe(self):
        self.start('11-browser-errors-and-network')
        errors = self.browser('errors', '--json')
        payload = json.loads(errors['stdout'])
        self.current['expected'].append('No browser page errors during prototype suite')
        self.current['actual'].append(payload)
        if not payload.get('success') or payload.get('data', {}).get('errors') != []:
            raise AssertionError(f'Browser errors reported: {payload}')
        network = self.browser('network', 'requests', '--json')
        net_payload = json.loads(network['stdout'])
        reqs = net_payload.get('data', {}).get('requests', [])
        self.current['expected'].append('All captured session requests stay on local file:// URLs')
        self.current['actual'].append({'success': net_payload.get('success'), 'urls': [r.get('url', '') for r in reqs]})
        if not net_payload.get('success') or not all(r.get('url', '').startswith('file://') for r in reqs):
            raise AssertionError(f'Unexpected browser network requests: {net_payload}')
        self.report['network_observation'] = {
            'scope': 'agent-browser captured page requests for this isolated browser session only; not all host traffic',
            'receipt': network,
        }
        self.finish()

    def cleanup(self):
        receipt = {'session': self.session, 'attempted': self.browser_started}
        if self.browser_started:
            result = self.command([self.binary, '--session', self.session, '--idle-timeout', '120000', 'close'], True)
            receipt.update(exit_code=result['exit_code'], stdout=result['stdout'], stderr=result['stderr'],
                           status='PASS' if result['exit_code'] == 0 else 'FAIL')
        else:
            receipt['status'] = 'N/A'
        self.report['cleanup'] = receipt
        write_json(self.evidence / 'cleanup-receipt.json', receipt)


def interrupted(signum, _frame):
    raise RuntimeError(f'Interrupted by signal {signum}')


def doctor(args):
    path = Path(args.prototype).expanduser().resolve()
    binary = artifact_check(path)
    receipt = subprocess.run([binary, '--version'], text=True, capture_output=True, timeout=15)
    if receipt.returncode:
        raise Blocked(f'agent-browser unavailable: {receipt.stderr}')
    print(json.dumps({'status': 'PASS', 'prototype': str(path), 'sha256': sha(path),
                      'agent_browser': receipt.stdout.strip(), 'page_usability': 'NOT_RUN',
                      'note': 'Read-only static preflight. Suite opens the page and checks usable initial UI.'}, ensure_ascii=False))
    return 0


def suite(args):
    if args.timeout <= 0:
        raise Blocked('--timeout must be a positive integer.')
    run = Run(Path(args.prototype).expanduser().resolve(), safe_run_dir(args.run_dir))
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
        signal.signal(sig, interrupted)
    signal.alarm(args.timeout)
    try:
        run.prepare()
        run.search()
        run.locate()
        run.shared()
        run.panel_search()
        run.roundtrip()
        target = run.handoff()
        run.changed_history(target)
        run.reset_check()
        run.responsive()
        run.observe()
        run.report['result'] = 'PASS'
    except Exception as exc:
        status = 'BLOCKED' if isinstance(exc, Blocked) else 'FAIL'
        run.report.update(result=status, failure=str(exc))
        if run.current:
            run.current.update(status=status, failure=str(exc))
            if run.browser_started:
                try:
                    run.capture('-failure')
                except Exception as capture_error:
                    run.current['capture_error'] = str(capture_error)
    finally:
        signal.alarm(0)
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGALRM):
            signal.signal(sig, signal.SIG_DFL)
        run.current = None
        try:
            run.cleanup()
        except Exception as exc:
            run.report['cleanup'] = {'status': 'FAIL', 'error': str(exc), 'session': run.session}
            try:
                write_json(run.evidence / 'cleanup-receipt.json', run.report['cleanup'])
            except OSError:
                pass
        run.report['prototype']['sha256_after'] = safe_sha(run.prototype)
        baseline = run.report['prototype'].get('sha256_before')
        unchanged = baseline == run.report['prototype'].get('sha256_after') if baseline else None
        run.report['prototype']['unchanged'] = unchanged
        script_after = safe_sha(SCRIPT)
        run.report['harness_sha256_after'] = script_after
        run.report['harness_unchanged'] = script_after is not None and run.script_sha == script_after
        if unchanged is False or not run.report['harness_unchanged'] or run.report['cleanup']['status'] == 'FAIL':
            reason = 'Artifact/harness changed during run or cleanup failed'
            run.report.update(result='FAIL', invalidated_reason=reason, failure=run.report.get('failure') or reason)
        write_json(run.evidence / 'suite-summary.json', run.report)
    print(json.dumps({'result': run.report['result'], 'run_dir': str(run.root),
                      'scenarios': len(run.report['scenarios']), 'failure': run.report.get('failure'),
                      'summary': str(run.evidence / 'suite-summary.json')}, ensure_ascii=False))
    return 0 if run.report['result'] == 'PASS' else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('doctor', 'suite'):
        command = sub.add_parser(name)
        command.add_argument('--prototype', required=True, help='External Main.html; never copied into repository')
        if name == 'suite':
            command.add_argument('--run-dir')
            command.add_argument('--timeout', type=int, default=900, help='Total suite timeout in seconds')
    args = parser.parse_args()
    try:
        return doctor(args) if args.command == 'doctor' else suite(args)
    except (Blocked, OSError, subprocess.TimeoutExpired) as exc:
        print(json.dumps({'result': 'BLOCKED', 'failure': str(exc)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    sys.exit(main())
