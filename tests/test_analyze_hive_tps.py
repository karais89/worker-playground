"""Exercise the offline TPS analyzer with synthetic records; no model calls."""
import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'benchmarks' / 'analyze_hive_tps.py'
MODEL = 'deepseek-ai/deepseek-v4.1-flash'


class AnalyzeHiveTpsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def failed_row(self, index=1, **overrides):
        row = dict(index=index, model=MODEL, workload='sequence', effort='low',
                   trial=index, warmup=False, http_status=500, error='HTTP failure')
        row.update(overrides)
        return row

    def successful_row(self, index=1, seconds=2.0):
        output = ' '.join(str(value) for value in range(1, 601))
        filename = f'{index}-output.txt'
        (self.directory / filename).write_text(output, encoding='utf-8')
        return dict(index=index, model=MODEL, workload='sequence', effort='low',
                    trial=index, warmup=False, http_status=200, stream_done=True,
                    finish_reason='stop', malformed_events=0, output_valid=True,
                    output_file=filename, total_seconds=seconds,
                    completion_tps_end_to_end=100 / seconds,
                    usage=dict(prompt_tokens=10, completion_tokens=100,
                               prompt_tokens_details=dict(cached_tokens=0)))

    def analyze(self, rows, finished=True):
        result = dict(rows=rows)
        if finished:
            result['finished_utc'] = '2026-10-05T00:00:00Z'
        source = self.directory / 'results.json'
        source.write_text(json.dumps(result), encoding='utf-8')
        original = source.read_bytes()
        process = subprocess.run([sys.executable, str(SCRIPT), str(self.directory)],
                                 capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(source.read_bytes(), original, 'Raw results must not be rewritten')
        return process

    def outputs(self):
        analysis = json.loads((self.directory / 'analysis.json').read_text(encoding='utf-8'))
        with (self.directory / 'measurements.csv').open(encoding='utf-8-sig', newline='') as stream:
            measurements = list(csv.DictReader(stream))
        summary = next(row for row in analysis['summaries']
                       if (row['model'], row['workload'], row['effort']) == (MODEL, 'sequence', 'low'))
        return analysis, measurements, summary

    def test_success_keeps_metrics_and_excludes_warmup(self):
        row = self.successful_row()
        original_output = (self.directory / row['output_file']).read_bytes()
        warmup = self.failed_row(index=0, warmup=True, workload='warmup')
        process = self.analyze([warmup, row])
        self.assertEqual(process.returncode, 0, process.stderr)
        analysis, measurements, summary = self.outputs()
        self.assertEqual(analysis['valid_requests'], 1)
        self.assertEqual(len(analysis['rows']), 1)
        self.assertIs(analysis['rows'][0]['eligible'], True)
        self.assertEqual(summary['attempts'], 1)
        self.assertEqual(summary['completion_tps_end_to_end'], 50.0)
        self.assertEqual(summary['total_seconds_range'], [2.0, 2.0])
        self.assertEqual(summary['cached_prompt_tokens'], [0])
        self.assertEqual([row['eligible'] for row in measurements], ['True'])
        self.assertEqual((self.directory / row['output_file']).read_bytes(), original_output)

    def assert_failed_record_is_retained(self, row):
        process = self.analyze([row])
        self.assertEqual(process.returncode, 0, process.stderr)
        analysis, measurements, summary = self.outputs()
        self.assertEqual(analysis['rows'], [dict(row, eligible=False)])
        self.assertEqual(analysis['valid_requests'], 0)
        self.assertEqual((summary['attempts'], summary['valid']), (1, 0))
        self.assertIsNone(summary['completion_tps_end_to_end'])
        self.assertIsNone(summary['total_seconds_range'])
        self.assertEqual([record['eligible'] for record in measurements], ['False'])

    def test_http_failure_is_retained_and_excluded(self):
        self.assert_failed_record_is_retained(self.failed_row())

    def test_timeout_is_retained_and_excluded(self):
        self.assert_failed_record_is_retained(
            self.failed_row(http_status=200, error='TimeoutError', total_seconds=180.0))

    def test_mixed_records_aggregate_only_successful_samples(self):
        rows = [self.successful_row(index=1, seconds=2.0), self.failed_row(index=2),
                self.failed_row(index=3, http_status=200, error='TimeoutError', total_seconds=180.0),
                self.successful_row(index=4, seconds=4.0)]
        process = self.analyze(rows)
        self.assertEqual(process.returncode, 0, process.stderr)
        analysis, measurements, summary = self.outputs()
        self.assertEqual(analysis['valid_requests'], 2)
        self.assertEqual(len(analysis['rows']), 4)
        self.assertEqual((summary['attempts'], summary['valid']), (4, 2))
        self.assertEqual(summary['total_seconds'], 3.0)
        self.assertEqual(summary['total_seconds_range'], [2.0, 4.0])
        self.assertEqual(summary['completion_tps_end_to_end'], 37.5)
        self.assertEqual([row['eligible'] for row in measurements], ['True', 'False', 'False', 'True'])
        self.assertEqual([row.get('error') for row in analysis['rows']],
                         [None, 'HTTP failure', 'TimeoutError', None])

    def test_all_failed_records_produce_empty_statistics(self):
        rows = [self.failed_row(), self.failed_row(index=2, error='TimeoutError')]
        process = self.analyze(rows)
        self.assertEqual(process.returncode, 0, process.stderr)
        analysis, measurements, summary = self.outputs()
        self.assertEqual(analysis['valid_requests'], 0)
        self.assertEqual((summary['attempts'], summary['valid']), (2, 0))
        self.assertIsNone(summary['total_seconds'])
        self.assertIsNone(summary['completion_tps_end_to_end'])
        self.assertEqual(len(measurements), 2)

    def test_missing_output_cannot_reuse_stale_eligibility(self):
        self.assert_failed_record_is_retained(self.failed_row(eligible=True))

    def test_unfinished_run_is_still_rejected(self):
        process = self.analyze([self.successful_row()], finished=False)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('Run has not finished; refusing partial analysis.', process.stderr)
        self.assertFalse((self.directory / 'analysis.json').exists())
        self.assertFalse((self.directory / 'measurements.csv').exists())


if __name__ == '__main__':
    unittest.main()
