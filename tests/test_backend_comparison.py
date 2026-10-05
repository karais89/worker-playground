import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks import backend_comparison as comparison


class BackendComparisonTests(unittest.TestCase):
    def rows(self):
        common = dict(case='case',eligible=True,actual_delegation=True,passed=True,
                      main_usage=dict(total_tokens=100),worker_usage=dict(total_tokens=200))
        return [dict(common,backend='codex',seconds=10),dict(common,backend='opencode',seconds=8)]

    def test_paired_metrics_and_failures_are_separate(self):
        rows=self.rows()
        result=comparison.summarize(rows,[])['comparisons'][0]
        self.assertTrue(result['comparable'])
        self.assertAlmostEqual(result['opencode_time_change'],-.2)
        self.assertEqual(result['total_tokens_median']['opencode'],300)
        rows[1]['eligible']=False
        result=comparison.summarize(rows,[])['comparisons'][0]
        self.assertFalse(result['comparable'])
        self.assertIsNone(result['opencode_time_change'])
        self.assertEqual(result['seconds_median']['opencode'],8)

    def test_unmatched_runs_or_zero_delegation_have_no_comparison(self):
        rows=self.rows()
        for selected in (rows[:1],rows+[rows[0]], [dict(r,actual_delegation=False) for r in rows]):
            result=comparison.summarize(selected,[])['comparisons'][0]
            self.assertFalse(result['comparable'])
            self.assertIsNone(result['opencode_time_change'])

    def test_only_selected_auth_is_loaded_and_profile_is_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            source=root/'auth.json'
            source.write_text('{"fixture":"codex credential"}')
            settings=({'models':{}},{'hive-ai':{'type':'api','key':'fixture-only'}})
            with patch.dict(os.environ,{'OPENAI_API_KEY':'never inherit'}):
                with comparison.profile(root,source,settings) as (home,env):
                    self.assertNotIn('OPENAI_API_KEY',env)
                    self.assertEqual(json.loads((home/'data/opencode/auth.json').read_text()),settings[1])
                    config=json.loads(env['OPENCODE_CONFIG_CONTENT'])
                    self.assertEqual(config['enabled_providers'],['hive-ai'])
                    self.assertEqual(list(config['provider']),['hive-ai'])
                    self.assertEqual(config['share'],'disabled')
            self.assertFalse(home.exists())

    def test_source_settings_does_not_copy_other_provider_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'auth.json').write_text(json.dumps({'hive-ai':{'key':'fixture'},'unrelated':{'key':'excluded'}}))
            config=dict(provider={'hive-ai':dict(models={'deepseek-ai/deepseek-v4.1-flash':{}}),'unrelated':{}})
            replies=[subprocess.CompletedProcess([],0,json.dumps(config),''),
                     subprocess.CompletedProcess([],0,'data '+str(root)+'\n','')]
            with patch.object(comparison.subprocess,'run',side_effect=replies):
                provider,credentials=comparison.source_settings(['fixture'])
            self.assertEqual(credentials,{'hive-ai':{'key':'fixture'}})
            self.assertEqual(provider,config['provider']['hive-ai'])

    def test_large_export_retains_only_metadata_without_pipe_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'session.json').write_text(json.dumps(dict(backend='opencode',session_id='fixture-session')))
            script=root/'export.py'
            script.write_text('import json, os, stat, sys\n'
                'assert sys.argv[1:] == ["export", "fixture-session"]\n'
                'assert stat.S_ISREG(os.fstat(sys.stdout.fileno()).st_mode)\n'
                'print(json.dumps({"messages":[{"info":{"role":"assistant",'
                '"modelID":"model","providerID":"provider","variant":"max",'
                '"finish":"stop","tokens":{"total":42}},"parts":[{"text":"x"*150000}]}]}))\n')
            result=comparison.opencode_contexts([sys.executable,str(script)],dict(os.environ),root)
            self.assertTrue(result[0]['exported'])
            self.assertEqual(result[0]['messages'][0]['modelID'],'model')
            self.assertEqual(result[0]['messages'][0]['tokens'],{'total':42})
            self.assertLess(len(json.dumps(result)),1000)
            self.assertEqual({p.name for p in root.iterdir()},{'session.json','export.py'})


if __name__=='__main__':
    unittest.main()
