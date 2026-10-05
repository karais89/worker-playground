"""Analyze a Hive speed run without executing generated source files."""
import ast
import csv
import json
import statistics
import sys
from pathlib import Path

NAMES = {
    'clamp', 'chunked', 'unique_preserve_order', 'flatten_once', 'merge_counts',
    'moving_average', 'binary_search', 'gcd', 'lcm', 'primes_up_to',
    'safe_divide', 'rotate',
}


def median(rows, key):
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return round(statistics.median(values), 3) if values else None


def main():
    directory = Path(sys.argv[1]).resolve()
    result = json.loads((directory / 'results.json').read_text(encoding='utf-8'))
    if not result.get('finished_utc'):
        raise SystemExit('Run has not finished; refusing partial analysis.')
    rows = [row.copy() for row in result['rows'] if not row['warmup']]
    for row in rows:
        # Failed requests may have no output file; retain them but exclude their metrics.
        row['eligible'] = False
        if not row.get('output_file'):
            continue
        output = (directory / row['output_file']).read_text(encoding='utf-8')
        row['output_characters'] = len(output)
        row['characters_per_second_end_to_end'] = round(len(output) / row['total_seconds'], 3)
        if row['workload'] == 'python':
            source = output.strip()
            row['format_compliant'] = not source.startswith('```')
            # Keep the raw artifact and its formatting violation; only remove
            # a complete outer fence for the separate code syntax check.
            if source.startswith('```') and source.endswith('```'):
                source = '\n'.join(source.splitlines()[1:-1])
            try:
                tree = ast.parse(source)
                names = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
                assertions = sum(
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr.startswith('assert')
                    for node in ast.walk(tree)
                )
                row['syntax_valid'] = True
                row['required_functions_present'] = NAMES <= names
                row['unittest_assertion_count'] = assertions
                row['output_valid'] = NAMES <= names and assertions >= 24
            except SyntaxError:
                row['syntax_valid'] = False
                row['output_valid'] = False
        row['eligible'] = (
            row.get('http_status') == 200 and row.get('stream_done') is True
            and row.get('finish_reason') == 'stop' and row.get('output_valid') is True
            and row.get('malformed_events') == 0 and row.get('usage') is not None
        )
    summaries = []
    metrics = (
        'completion_tps_end_to_end', 'visible_tps_end_to_end',
        'visible_stream_tps_estimate', 'generated_stream_tps_estimate',
        'first_generated_seconds', 'first_visible_seconds', 'total_seconds',
        'reasoning_tokens', 'visible_tokens', 'characters_per_second_end_to_end',
    )
    for workload in ['sequence', 'python']:
        for effort in ['low', 'max']:
            for model in ['deepseek-ai/deepseek-v4.1-flash', 'zai-org/glm-5.3-flash']:
                selected = [row for row in rows if row['model'] == model and row['workload'] == workload and row['effort'] == effort]
                valid = [row for row in selected if row['eligible']]
                summary = {'workload': workload, 'effort': effort, 'model': model, 'attempts': len(selected), 'valid': len(valid)}
                summary.update({key: median(valid, key) for key in metrics})
                summary['total_seconds_range'] = [min(row['total_seconds'] for row in valid), max(row['total_seconds'] for row in valid)] if valid else None
                summary['cached_prompt_tokens'] = [row['usage'].get('prompt_tokens_details', {}).get('cached_tokens') for row in valid]
                summaries.append(summary)
    # Conservative estimate using the larger published price when a page shows
    # both original and discounted rates. This is not the actual account charge.
    upper_estimate = 0
    for row in result['rows']:
        usage = row.get('usage') or {}
        input_rate, output_rate = (0.30, 1.20) if row['model'].startswith('deepseek') else (0.15, 0.50)
        upper_estimate += (usage.get('prompt_tokens', 0) * input_rate + usage.get('completion_tokens', 0) * output_rate) / 1_000_000
    analysis = {
        'source': 'results.json', 'completed_requests': len(rows),
        'valid_requests': sum(row['eligible'] for row in rows),
        'estimated_cost_usd_at_higher_published_rates': round(upper_estimate, 5),
        'quality_check_scope': 'Exact numeric sequence; Python syntax, twelve function names and >=24 unittest assertions after removing a complete outer Markdown fence if present. Raw output is preserved, and fence use is reported as format_compliant=false. Generated code was not executed and functional correctness is not established.',
        'summaries': summaries, 'rows': rows,
    }
    (directory / 'analysis.json').write_text(json.dumps(analysis, ensure_ascii=False, indent=2), encoding='utf-8')
    columns = ['index', 'model', 'workload', 'effort', 'trial', 'eligible', 'finish_reason', 'completion_tokens', 'reasoning_tokens', 'visible_tokens', 'total_seconds', 'first_visible_seconds', 'completion_tps_end_to_end', 'visible_tps_end_to_end', 'visible_stream_tps_estimate', 'generated_stream_tps_estimate', 'characters_per_second_end_to_end', 'output_characters', 'continuous_stream', 'largest_visible_batch_share', 'output_valid']
    with (directory / 'measurements.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({key: value for key, value in analysis.items() if key != 'rows'}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
