import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import opencode_backend as adapter
import team
import worker


FAKE_CLI = r'''
import json, os, pathlib, sys
args = sys.argv[1:]
prompt = sys.stdin.buffer.read().decode('utf-8')
if args[:2] == ['session','list']:
    assert args == ['session','list','--format','json','--max-count','0']
    if os.environ.get('FIXTURE_INIT_FAIL'):
        print('database is locked',file=sys.stderr)
        sys.exit(3)
    pathlib.Path(os.environ['FIXTURE_INIT_MARKER']).write_text('initialized')
    print('[]')
    sys.exit(0)
def emit(kind, **fields):
    print(json.dumps(dict(type=kind, **fields)), flush=True)
def report(status='completed', **extra):
    return dict(status=status, summary='done', changed_files=[], checks=[], issues=[], **extra)
if 'exec' in args:
    assert 'run' not in args and '--agent' not in args
    sid = args[args.index('resume') + 1] if 'resume' in args else 'main-session'
    if 'resume' not in args:
        final = dict(summary='delegate', report=None, tasks=[dict(id='code',role='implement',scope=['a.py'],prompt='Implement',acceptance=['fixed'])])
        usage = dict(input_tokens=100,cached_input_tokens=0,output_tokens=10)
    elif pathlib.Path('a.py').read_text() == 'draft':
        final = report('blocked',followups=[dict(id='code',prompt='Correct the defect')])
        usage = dict(input_tokens=160,cached_input_tokens=0,output_tokens=16)
    else:
        final = report(followups=[])
        usage = dict(input_tokens=210,cached_input_tokens=0,output_tokens=21)
    pathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(final))
    emit('thread.started', thread_id=sid)
    emit('turn.completed', usage=usage)
else:
    assert args[0]=='run' and '--output-schema' not in args and '--auto' not in args
    assert args[args.index('--model')+1]=='fixture/model'
    assert args[args.index('--variant')+1]=='high'
    assert args[args.index('--dir')+1]==str(pathlib.Path.cwd())
    name=args[args.index('--agent')+1]
    permissions=json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])['agent'][name]['permission']
    assert permissions['task']=='deny' and permissions['skill']=='deny'
    role=name.removeprefix('cli-worker-')
    assert permissions['bash']==('allow' if role=='implement' else 'deny')
    assert permissions['edit']==('allow' if role=='implement' else 'deny')
    sid=args[args.index('--session')+1] if '--session' in args else 'worker-session'
    settings = {}
    task_line=next((s[6:] for s in prompt.splitlines() if s.startswith('Task: ')), '')
    if task_line.startswith('{'): settings=json.loads(task_line)
    if settings.get('require_initialization'):
        assert pathlib.Path(os.environ['FIXTURE_INIT_MARKER']).read_text()=='initialized'
    if settings.get('delay'):
        import time
        time.sleep(settings['delay'])
    if role=='implement': pathlib.Path('a.py').write_text('fixed' if '--session' in args else 'draft')
    if settings.get('wrong_sid'): sid='wrong-session'
    if settings.get('malformed'): print('not json',flush=True)
    if settings.get('fake_final'):
        output=pathlib.Path(os.environ['FIXTURE_OUTPUT'])
        output.joinpath('final.json').write_text(json.dumps(report()))
    for n in (1,2):
        fields=dict(id='start'+str(n),messageID='msg'+str(n),sessionID=sid,type='step-start')
        emit('step_start', sessionID=sid, part=fields)
        if n==1:
            emit('tool_use',sessionID=sid,part=dict(id='tool',messageID='msg1',sessionID=sid,type='tool',tool='bash',state=dict(status='completed',input=dict(command='fixture-check'),metadata=dict(exit=0))))
        if n==2 and not settings.get('no_report'):
            final=report()
            if settings.get('bad_schema'): final['checks']=[dict(command='test',exit_code=True)]
            emit('text',sessionID=sid,part=dict(id='text',messageID='msg2',sessionID=sid,type='text',text=json.dumps(final)))
        tokens=dict(input=10 if n==1 else 20,output=3 if n==1 else 4,reasoning=2 if n==1 else 1,cache=dict(read=4 if n==1 else 5,write=2 if n==1 else 0))
        if settings.get('no_usage'): tokens=None
        emit('step_finish',sessionID=sid,part=dict(id='finish'+str(n),messageID='msg'+str(n),sessionID=sid,type='step-finish',reason='tool-calls' if n==1 else settings.get('reason','stop'),tokens=tokens))
    if settings.get('runtime_error'): emit('error',sessionID=sid,error=dict(name='FixtureError'))
    sys.exit(settings.get('exit',0))
'''


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='opencode-worker-test-')
        self.root = Path(self.temp.name).resolve()
        self.repo = self.root / 'repo with spaces'
        self.repo.mkdir()
        subprocess.run(['git', 'init', '-q', str(self.repo)], check=True)
        self.fake = self.root / 'fake cli.py'
        self.fake.write_text(FAKE_CLI, encoding='utf-8')
        self.cli = [sys.executable, str(self.fake)]
        self.env = dict(os.environ, OPENCODE_CONFIG_CONTENT='{"provider": {"fixture": {}}}')

    def tearDown(self):
        self.temp.cleanup()

    def task(self, **settings):
        return dict(id='code',role='implement',cwd=str(self.repo),scope=['a.py'],
                    prompt=json.dumps(settings),acceptance=['fixed'])

    def batch(self, **settings):
        return worker.run_batch([self.task(**settings)], self.root/'out',cli=self.cli,
                                model='fixture/model',backend='opencode',env=self.env)

    def test_real_subprocess_worker_and_resume_account_per_call_steps(self):
        batch = self.batch()
        self.assertEqual(batch['status'], 'completed')
        self.assertEqual(batch['usage']['total_tokens'], 51)
        saved = worker.read_json(self.root/'out/code/report.json')
        self.assertEqual(saved['usage']['reasoning_output_tokens'],3)
        self.assertEqual(saved['usage']['input_tokens'],41)
        self.assertEqual(saved['usage']['output_tokens'],10)
        self.assertEqual(saved['usage']['cached_input_tokens'],9)
        self.assertEqual(saved['usage']['cache_creation_input_tokens'],2)
        self.assertEqual(saved['observed_commands'],[dict(command='fixture-check',exit_code=0)])
        self.assertEqual(saved['observed_changed_files'],['a.py'])
        resumed = worker.resume_task(self.root/'out/code',self.root/'resume','한국어 correction',10,env=self.env)
        self.assertEqual(resumed['status'],'completed')
        self.assertEqual(resumed['usage']['total_tokens'],51)
        self.assertEqual(resumed['cumulative_usage']['total_tokens'],102)
        self.assertTrue(resumed['session_identity_ok'])
        session=worker.read_json(self.root/'resume/session.json')
        self.assertEqual(session['backend'],'opencode')
        self.assertEqual(session['model'],'fixture/model')
        self.assertEqual(session['session_id'],'worker-session')
        self.assertEqual(session['cli'],self.cli)
        self.assertEqual((self.repo/'a.py').read_text(),'fixed')
        self.assertIn('한국어', (self.root/'resume/prompt.txt').read_text(encoding='utf-8'))
        with self.assertRaisesRegex(ValueError,'newest result'):
            worker.resume_task(self.root/'out/code',self.root/'stale','again',10,env=self.env)

    def test_parallel_batch_initializes_shared_store_before_workers(self):
        env=dict(self.env,FIXTURE_INIT_MARKER=str(self.root/'initialized'))
        tasks=[dict(self.task(require_initialization=True),id=ident,role='research',scope=[ident+'.py'])
               for ident in ('one','two')]
        result=worker.run_batch(tasks,self.root/'out',cli=self.cli,model='fixture/model',
                                backend='opencode',env=env)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['usage']['total_tokens'],102)
        self.assertEqual(worker.read_json(self.root/'out/initialization/execution.json')['exit_code'],0)
        self.assertEqual((self.root/'out/initialization/events.jsonl').read_text().strip(),'[]')

    def test_initialization_failure_preserves_evidence_and_starts_no_workers(self):
        env=dict(self.env,FIXTURE_INIT_MARKER=str(self.root/'initialized'),FIXTURE_INIT_FAIL='1')
        tasks=[dict(self.task(),id=ident,role='research',scope=[ident+'.py']) for ident in ('one','two')]
        with self.assertRaisesRegex(ValueError,'shared store initialization failed'):
            worker.run_batch(tasks,self.root/'out',cli=self.cli,model='fixture/model',backend='opencode',env=env)
        self.assertEqual(worker.read_json(self.root/'out/initialization/execution.json')['exit_code'],3)
        self.assertFalse((self.root/'out/one').exists())
        self.assertFalse((self.root/'out/two').exists())

    def test_codex_main_opencode_worker_feedback_end_to_end(self):
        out=self.root/'team'
        result=team.run_team('Implement',self.repo,out,cli=self.cli,worker_cli=self.cli,
                             worker_backend='opencode',worker_model='fixture/model',env=self.env)
        self.assertEqual(result['status'],'completed')
        self.assertEqual(result['repair_invocations'],1)
        self.assertEqual(result['main_usage']['total_tokens'],231)
        self.assertEqual(result['worker_usage']['total_tokens'],102)
        self.assertEqual(result['worker_backend'],'opencode')
        for phase in ('plan','review','review-final'):
            cmd=worker.read_json(out/phase/'command.json')
            self.assertIn('exec',cmd)
            self.assertNotIn('--agent',cmd)
        for phase in ('workers','followups'):
            cmd=worker.read_json(out/phase/'code/command.json')
            self.assertIn('run',cmd)
            self.assertIn('cli-worker-implement',cmd)

    def test_invalid_model_fails_before_main_or_artifact_creation(self):
        for model in (None,'gpt-6.1-sol','/model','provider/','provider/a b'):
            with self.subTest(model=model), patch.object(team,'invoke') as main:
                with self.assertRaisesRegex(ValueError,'provider/model'):
                    team.run_team('task',self.repo,self.root/'out',worker_backend='opencode',worker_model=model)
                main.assert_not_called()
                self.assertFalse((self.root/'out').exists())

    def test_invalid_results_and_nonzero_exit_fail(self):
        for settings in (dict(no_report=True),dict(bad_schema=True),dict(reason='length'),
                         dict(malformed=True),dict(runtime_error=True),dict(exit=5)):
            with self.subTest(settings=settings):
                out=self.root/('case'+str(len(list(self.root.iterdir()))))
                result=worker.run_batch([self.task(**settings)],out,cli=self.cli,
                                        model='fixture/model',backend='opencode',env=self.env)
                self.assertEqual(result['status'],'failed')

    def test_unvalidated_artifact_cannot_replace_missing_final_response(self):
        self.env['FIXTURE_OUTPUT']=str(self.root/'out/code')
        self.assertEqual(self.batch(no_report=True,fake_final=True)['status'],'failed')

    def test_timeout_has_no_success_or_measured_usage(self):
        result=worker.run_batch([self.task(delay=3)],self.root/'out',cli=self.cli,
                                model='fixture/model',backend='opencode',timeout=.2,env=self.env)
        self.assertEqual(result['status'],'failed')
        saved=worker.read_json(self.root/'out/code/report.json')
        self.assertEqual(saved['execution']['termination'],'timeout')
        self.assertIsNone(result['usage'])

    def test_missing_tokens_remain_unknown(self):
        result=self.batch(no_usage=True)
        self.assertEqual(result['status'],'completed')
        self.assertIsNone(result['usage'])

    def test_resume_checks_profile_and_session_identity(self):
        self.batch(wrong_sid=True)
        changed=dict(self.env, XDG_DATA_HOME=str(self.root/'different'))
        with self.assertRaisesRegex(ValueError,'same OpenCode profile'):
            worker.resume_task(self.root/'out/code',self.root/'bad-profile','again',10,env=changed)
        session=worker.read_json(self.root/'out/code/session.json')
        session['session_id']='original-session'
        worker.write_json(self.root/'out/code/session.json',session)
        result=worker.resume_task(self.root/'out/code',self.root/'bad-identity','again',10,env=self.env)
        self.assertFalse(result['session_identity_ok'])
        self.assertEqual(result['status'],'failed')

    def test_inline_role_permissions_and_user_config_preservation(self):
        for role in worker.ROLES:
            source=dict(OPENCODE_CONFIG_CONTENT='{"provider":{"keep":{}},"agent":{"other":{"mode":"primary"}}}')
            result=adapter.prepare_env(source,role)
            config=json.loads(result['OPENCODE_CONFIG_CONTENT'])
            self.assertIn('keep',config['provider'])
            self.assertIn('other',config['agent'])
            permission=config['agent'][adapter.agent_name(role)]['permission']
            self.assertEqual(permission['edit'],'allow' if role=='implement' else 'deny')
            self.assertEqual(permission['bash'],'allow' if role=='implement' else 'deny')
            self.assertEqual(permission['*'],'deny')
            self.assertEqual(permission['task'],'deny')
            self.assertNotEqual(source,result)
        with self.assertRaises(ValueError): adapter.prepare_env({'OPENCODE_CONFIG_CONTENT':'[]'},'review')

    def test_empty_variant_is_omitted_and_command_preserves_paths(self):
        command=worker.build_command(self.cli,self.task(),'fixture/model','',self.root/'out',
                                     session_id='exact-session',backend='opencode')
        self.assertNotIn('--variant',command)
        self.assertIn(str(self.repo),command)
        self.assertEqual(command[command.index('--session')+1],'exact-session')

    @unittest.skipUnless(os.name=='nt','Windows npm shim resolution')
    def test_npm_shim_resolves_native_binary_without_shell(self):
        shim=self.root/'opencode.cmd'
        shim.write_text('fixture')
        native=self.root/'node_modules/opencode-ai/bin/opencode.exe'
        native.parent.mkdir(parents=True)
        native.write_bytes(b'fixture')
        with patch.object(adapter.shutil,'which',return_value=str(shim)):
            self.assertEqual(adapter.resolve_cli(['opencode']),[str(native)])
            native.unlink()
            with self.assertRaisesRegex(ValueError,'native binary'):
                adapter.resolve_cli(['opencode'])

    def test_cli_routes_only_workers_and_preserves_default(self):
        request=self.root/'request.txt'
        request.write_text('task',encoding='utf-8')
        result=dict(status='completed',worker_status=None)
        with patch.object(team,'run_team',return_value=result) as run, patch.object(sys,'stdout',io.StringIO()):
            self.assertEqual(team.main([str(request)]),0)
            self.assertEqual(run.call_args.args[3],['codex'])
            self.assertEqual(run.call_args.kwargs['worker_backend'],'codex')
            self.assertIsNone(run.call_args.kwargs['worker_cli'])
            self.assertEqual(team.main([str(request),'--worker-backend','opencode','--worker-model','fixture/model',
                                       '--opencode',str(self.fake),'--worker-effort','']),0)
            self.assertEqual(run.call_args.args[3],['codex'])
            self.assertEqual(run.call_args.args[5],'fixture/model')
            self.assertEqual(run.call_args.args[7],'')
            self.assertEqual(run.call_args.kwargs['worker_backend'],'opencode')
            self.assertEqual(run.call_args.kwargs['worker_cli'],[str(self.fake)])
        spec=self.root/'tasks.json'
        worker.write_json(spec,dict(tasks=[self.task()]))
        with patch.object(worker,'run_batch',return_value=result) as batch, patch.object(sys,'stdout',io.StringIO()):
            self.assertEqual(worker.main(['run',str(spec),'--backend','opencode','--model','fixture/model',
                                         '--opencode',str(self.fake)]),0)
            self.assertEqual(batch.call_args.args[2],[str(self.fake)])
            self.assertEqual(batch.call_args.kwargs['backend'],'opencode')

    def test_final_schema_only_and_no_earlier_report_fallback(self):
        report=dict(status='completed',summary='done',changed_files=[],checks=[],issues=[])
        text=json.dumps(report)
        self.assertEqual(adapter.final_report([text],worker.REPORT_SCHEMA),report)
        self.assertEqual(adapter.final_report(['```json\n'+text+'\n```'],worker.REPORT_SCHEMA),report)
        self.assertIsNone(adapter.final_report([text,' narration'],worker.REPORT_SCHEMA))
        self.assertIsNone(adapter.final_report([json.dumps(dict(report,extra=True))],worker.REPORT_SCHEMA))
        log=self.root/'events.jsonl'
        sid='one'
        records=[]
        for mid,reason,message in [('old','tool-calls',text),('final','stop','invalid final')]:
            for kind,part in [('step_start',dict(id=mid+'s',type='step-start')),
                              ('text',dict(id=mid+'t',type='text',text=message)),
                              ('step_finish',dict(id=mid+'f',type='step-finish',reason=reason,
                               tokens=dict(input=1,output=1,reasoning=0,cache=dict(read=0,write=0))))]:
                records.append(dict(type=kind,sessionID=sid,part=dict(part,sessionID=sid,messageID=mid)))
        log.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf-8')
        parsed=adapter.parse_events(log)
        self.assertEqual(parsed['messages'],['invalid final'])
        self.assertIsNone(adapter.final_report(parsed['messages'],worker.REPORT_SCHEMA))
        # Duplicate completed parts must not inflate usage.
        log.write_text('\n'.join(json.dumps(r) for r in records+records),encoding='utf-8')
        self.assertEqual(adapter.parse_events(log)['usage']['total_tokens'],4)
        incomplete=self.root/'incomplete.jsonl'
        incomplete.write_text('\n'.join(json.dumps(r) for r in records[1:]),encoding='utf-8')
        self.assertIn('incomplete OpenCode steps',adapter.parse_events(incomplete)['errors'])
        records[-1]['sessionID']='two'
        records[-1]['part']['sessionID']='two'
        log.write_text('\n'.join(json.dumps(r) for r in records),encoding='utf-8')
        self.assertIsNone(adapter.parse_events(log)['usage'])
        self.assertTrue(adapter.parse_events(log)['errors'])


if __name__ == '__main__':
    unittest.main()
