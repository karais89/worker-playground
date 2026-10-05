// Run with Node 24: node benchmarks/hive_tps.cjs <output-directory>
// Reads Hive credentials from OpenCode auth.json; never stores keys or headers.
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { performance } = require('node:perf_hooks');
const { randomUUID } = require('node:crypto');

const models = ['deepseek-ai/deepseek-v4.1-flash', 'zai-org/glm-5.3-flash'];
const workloads = {
  sequence: 'Output all integers from 1 through 600 in ascending order, separated by one space. Output only the integers, without omissions, code, or explanations.',
  python: `Write one complete Python 3 source file, with no Markdown fences or prose outside the file. Implement these twelve functions with type hints and a short docstring each:
clamp(value, minimum, maximum): clamp value to inclusive bounds; raise ValueError if minimum > maximum.
chunked(items, size): return a list of lists, final chunk may be shorter; raise ValueError if size <= 0.
unique_preserve_order(items): return first occurrences, preserving order; assume hashable items.
flatten_once(groups): concatenate a sequence of iterables into a list.
merge_counts(*mappings): add counts with matching keys into a new dictionary.
moving_average(values, window): list of full-window averages; raise ValueError if window <= 0.
binary_search(values, target): index of target in sorted values or -1; any matching index is valid.
gcd(a, b): nonnegative Euclidean gcd; gcd(0, 0) = 0.
lcm(a, b): nonnegative lcm; return 0 if either operand is 0.
primes_up_to(limit): sorted primes <= limit using a sieve; empty if limit < 2.
safe_divide(numerator, denominator, default=None): default for denominator zero, else division.
rotate(items, steps): return new list rotated right; allow negative steps and empty lists.
After the implementations add a unittest.TestCase with at least two assertions for each function, including empty/zero or error cases where appropriate, and a main guard running unittest.main(). Use only the Python standard library. Keep the complete source below 250 lines.`,
};
const functionNames = ['clamp', 'chunked', 'unique_preserve_order', 'flatten_once', 'merge_counts', 'moving_average', 'binary_search', 'gcd', 'lcm', 'primes_up_to', 'safe_divide', 'rotate'];
const round = value => value == null ? null : Math.round(value * 1000) / 1000;
const median = values => {
  const valid = values.filter(Number.isFinite).sort((a, b) => a - b);
  if (!valid.length) return null;
  const mid = Math.floor(valid.length / 2);
  return valid.length % 2 ? valid[mid] : (valid[mid - 1] + valid[mid]) / 2;
};

async function measure(key, job, prompt, outDir, maxTokens = 32000) {
  const row = { ...job, timestamp_utc: new Date().toISOString() };
  const start = performance.now();
  let firstGenerated = null, lastGenerated = null, firstVisible = null, lastVisible = null;
  let usage = null, output = '', finishReason = null, returnedModel = null, done = false;
  let buffer = '', readIndex = 0, malformedEvents = 0, visibleEvents = 0, reasoningEvents = 0;
  const visibleBatches = [], generatedBatches = [];
  try {
    const response = await fetch('https://api-cdn.thehive.ai/api/v3/chat/completions', {
      method: 'POST', headers: { authorization: `Bearer ${key}`, 'content-type': 'application/json', accept: 'text/event-stream' },
      body: JSON.stringify({ model: job.model, messages: [{ role: 'user', content: prompt }], stream: true, temperature: 0, reasoning_effort: job.effort, max_completion_tokens: maxTokens }),
      signal: AbortSignal.timeout(180000),
    });
    row.http_status = response.status;
    row.headers_seconds = round((performance.now() - start) / 1000);
    row.content_type = response.headers.get('content-type');
    if (!response.ok) { await response.body.cancel(); return { ...row, error: 'HTTP failure' }; }
    const reader = response.body.getReader(), decoder = new TextDecoder();
    function event(line, now) {
      if (!line.startsWith('data:')) return;
      const data = line.slice(5).trim();
      if (!data) return;
      if (data === '[DONE]') { done = true; return; }
      let chunk;
      try { chunk = JSON.parse(data); } catch { malformedEvents++; return; }
      returnedModel = chunk.model || returnedModel;
      if (chunk.usage) usage = chunk.usage;
      for (const choice of chunk.choices || []) {
        finishReason = choice.finish_reason || finishReason;
        const delta = choice.delta || {};
        const visible = typeof delta.content === 'string' ? delta.content : '';
        const reasoning = delta.reasoning_content || delta.reasoning || '';
        if (reasoning) reasoningEvents++;
        if (reasoning || visible) {
          firstGenerated ??= now; lastGenerated = now;
          const previous = generatedBatches.at(-1);
          if (previous?.read === readIndex) previous.characters += visible.length + reasoning.length;
          else generatedBatches.push({ read: readIndex, seconds: round((now - start) / 1000), characters: visible.length + reasoning.length });
        }
        if (visible) {
          visibleEvents++; firstVisible ??= now; lastVisible = now; output += visible;
          const previous = visibleBatches.at(-1);
          if (previous?.read === readIndex) previous.characters += visible.length;
          else visibleBatches.push({ read: readIndex, seconds: round((now - start) / 1000), characters: visible.length });
        }
      }
    }
    while (true) {
      const part = await reader.read();
      if (part.done) break;
      const now = performance.now(); readIndex++;
      buffer += decoder.decode(part.value, { stream: true });
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) {
        event(buffer.slice(0, index).replace(/\r$/, ''), now);
        buffer = buffer.slice(index + 1);
      }
    }
    if (buffer.trim()) event(buffer.trim(), performance.now());
    const end = performance.now();
    const completion = usage?.completion_tokens ?? null;
    const reasoning = usage?.completion_tokens_details?.reasoning_tokens ?? null;
    const visible = completion == null || reasoning == null ? null : completion - reasoning;
    const elapsed = (end - start) / 1000;
    const visibleSpan = firstVisible == null ? null : (lastVisible - firstVisible) / 1000;
    const generatedSpan = firstGenerated == null ? null : (lastGenerated - firstGenerated) / 1000;
    const largestBatchShare = output.length ? Math.max(...visibleBatches.map(batch => batch.characters)) / output.length : null;
    // A fast delivery burst cannot establish backend decoding speed.
    const continuous = visibleSpan >= 1 && visibleBatches.length >= 10 && largestBatchShare <= 0.2;
    const allChars = generatedBatches.reduce((sum, batch) => sum + batch.characters, 0);
    const generatedLargestShare = allChars ? Math.max(...generatedBatches.map(batch => batch.characters)) / allChars : null;
    const generatedContinuous = generatedSpan >= 1 && generatedBatches.length >= 10 && generatedLargestShare <= 0.2 && (reasoning === 0 || reasoningEvents > 0);
    Object.assign(row, {
      returned_model: returnedModel, finish_reason: finishReason, stream_done: done, malformed_events: malformedEvents,
      usage, completion_tokens: completion, reasoning_tokens: reasoning, visible_tokens: visible,
      first_generated_seconds: round(firstGenerated == null ? null : (firstGenerated - start) / 1000),
      first_visible_seconds: round(firstVisible == null ? null : (firstVisible - start) / 1000), total_seconds: round(elapsed),
      visible_stream_seconds: round(visibleSpan), visible_event_count: visibleEvents, reasoning_event_count: reasoningEvents,
      visible_delivery_batch_count: visibleBatches.length, largest_visible_batch_share: round(largestBatchShare), continuous_stream: continuous,
      completion_tps_end_to_end: round(completion == null ? null : completion / elapsed),
      visible_tps_end_to_end: round(visible == null ? null : visible / elapsed),
      visible_stream_tps_estimate: round(continuous && visible > 1 ? (visible - 1) / visibleSpan : null),
      generated_stream_tps_estimate: round(generatedContinuous && completion > 1 ? (completion - 1) / generatedSpan : null),
      visible_batches: visibleBatches,
    });
    if (job.workload === 'sequence') {
      const expected = Array.from({ length: 600 }, (_, i) => String(i + 1)).join(' ');
      row.output_valid = output.trim().split(/\s+/).join(' ') === expected;
    } else if (job.workload === 'python') {
      row.required_functions_present = functionNames.every(name => new RegExp(`^def ${name}\\(`, 'm').test(output));
      row.output_valid = null; // Set by independent AST validation after the run.
    } else row.output_valid = output.includes('OK');
    if (!job.warmup) {
      row.output_file = `${job.index}-${job.model.startsWith('deepseek') ? 'deepseek' : 'glm'}-${job.effort}-${job.workload}-${job.trial}.${job.workload === 'python' ? 'py' : 'txt'}`;
      fs.writeFileSync(path.join(outDir, row.output_file), output);
    }
    return row;
  } catch (error) { return { ...row, error: error.name, total_seconds: round((performance.now() - start) / 1000) }; }
}

function summarize(rows) {
  const summaries = [];
  for (const workload of Object.keys(workloads)) for (const effort of ['low', 'max']) for (const model of models) {
    const trials = rows.filter(row => !row.warmup && row.model === model && row.effort === effort && row.workload === workload);
    const complete = trials.filter(row => row.http_status === 200 && row.stream_done && row.finish_reason === 'stop' && row.usage);
    const summary = { model, workload, effort, attempted: trials.length, completed: complete.length, valid: complete.filter(row => row.output_valid === true).length };
    for (const metric of ['completion_tps_end_to_end', 'visible_tps_end_to_end', 'visible_stream_tps_estimate', 'generated_stream_tps_estimate', 'first_generated_seconds', 'first_visible_seconds', 'total_seconds', 'reasoning_tokens', 'visible_tokens']) {
      const values = complete.map(row => row[metric]).filter(Number.isFinite);
      summary[metric] = { median: round(median(values)), minimum: values.length ? Math.min(...values) : null, maximum: values.length ? Math.max(...values) : null, samples: values.length };
    }
    summaries.push(summary);
  }
  return summaries;
}

async function main() {
  const outDir = path.resolve(process.argv[2] || 'bench-runs/hive-tps');
  fs.mkdirSync(outDir, { recursive: true });
  if (fs.existsSync(path.join(outDir, 'results.json'))) throw new Error('OutputAlreadyExists');
  const key = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.local/share/opencode/auth.json'), 'utf8'))['hive-ai'].key;
  const runId = randomUUID();
  const result = {
    started_utc: new Date().toISOString(), endpoint: 'https://api-cdn.thehive.ai/api/v3/chat/completions',
    settings: { temperature: 0, max_completion_tokens: 32000, efforts: ['low', 'max'], repetitions: 3, concurrency: 1, warmups: 2 },
    methodology: 'Sequential matched pairs, alternate model order across cells/repetitions. Prefix each matched prompt with identical unique run/cell nonce to reduce exact-prompt cache reuse; server cache cannot be disabled or controlled. Two workloads, two effort values, three repetitions. All timing uses monotonic client clock. Total TPS includes reasoning, prefill, queue and network latency. Streaming estimates are emitted only for distributed delivery (>=1 s, >=10 reads, no read >20% of visible characters), and cannot prove backend decoding speed. Models use different tokenizers. Warmups excluded. No retries. Code correctness is outside this speed benchmark; independent Python AST syntax/structure checks only.',
    prompts: workloads, run_id: runId, rows: [], summaries: [],
  };
  const save = () => { result.summaries = summarize(result.rows); fs.writeFileSync(path.join(outDir, 'results.json'), JSON.stringify(result, null, 2)); };
  let index = 0;
  for (const model of models) {
    const row = await measure(key, { index: ++index, model, effort: 'low', workload: 'warmup', trial: 0, warmup: true }, 'Reply only OK.', outDir, 128);
    result.rows.push(row); save(); console.log(JSON.stringify({ warmup: model, status: row.http_status, seconds: row.total_seconds }));
  }
  let cell = 0;
  for (let trial = 1; trial <= 3; trial++) {
    const tasks = trial % 2 ? ['sequence', 'python'] : ['python', 'sequence'];
    for (const workload of tasks) for (const effort of trial % 2 ? ['low', 'max'] : ['max', 'low']) {
      const order = cell++ % 2 ? [...models].reverse() : models;
      const prompt = `Benchmark request ${runId}, cell ${cell}. Ignore this identifier in your output.\n\n${workloads[workload]}`;
      for (const model of order) {
        const row = await measure(key, { index: ++index, model, effort, workload, trial, warmup: false }, prompt, outDir);
        result.rows.push(row); save();
        console.log(JSON.stringify({ completed: result.rows.length - 2, of: 24, model, effort, workload, trial, status: row.http_status, finish: row.finish_reason, seconds: row.total_seconds, completion_tps: row.completion_tps_end_to_end, visible_tps: row.visible_tps_end_to_end, stream_tps: row.visible_stream_tps_estimate, reasoning_tokens: row.reasoning_tokens, visible_tokens: row.visible_tokens, valid: row.output_valid, error: row.error }));
        if (row.http_status === 401 || row.http_status === 405) { result.stopped_reason = 'Authentication or balance failure'; save(); return; }
      }
    }
  }
  result.finished_utc = new Date().toISOString(); save();
  console.log('FINISHED ' + path.join(outDir, 'results.json'));
}
if (require.main === module) main().catch(error => { console.error(error.name); process.exitCode = 1; });
module.exports = { summarize, median };
