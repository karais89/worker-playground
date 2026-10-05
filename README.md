# CLI Worker Playground

A small Python coordinator with a Codex CLI main and selectable Codex or OpenCode CLI workers: delegate a task, review the result, and resume the same workers for one correction round. No server or agent framework. Requires Python 3.11+, Git and an authenticated Codex CLI; OpenCode workers additionally require authenticated OpenCode. Codex integration targets CLI 0.160.0, OpenCode integration targets CLI 1.18.34; model availability depends on your provider/account. MIT licensed.

## Install as a Codex skill / 스킬 설치

```sh
git clone https://github.com/karais89/worker-playground.git
cd worker-playground
python scripts/install_skill.py
```

The installer copies an allowlisted, self-contained snapshot into `$CODEX_HOME/skills/cli-worker-team` (default: `~/.codex/skills/cli-worker-team`). It does not copy credentials, logs or benchmark data. The installed skill works without this checkout. Installation refuses an existing destination; preserve customizations and `runs/` before removing an old installation to reinstall, or choose a new `--destination` for inspection.

새 Codex 세션에서 작업할 Git 프로젝트를 열고 다음처럼 요청합니다.

```text
$cli-worker-team 로그인 오류를 수정하고 관련 테스트를 실행해줘.
기존 공개 API는 유지해줘.
```

스킬은 현재 채팅과 별도로 **CLI 메인 + CLI 워커**를 실행합니다. 기본 안내는 검증한 Sol 6.1 High / Sol 6.1 High이며, 요청에 메인·워커 모델을 지정하면 그 선택을 사용합니다. 워커 수는 기본 1명, 독립 작업은 최대 3명입니다. 설치 후 Codex에서 아직 보이지 않으면 새 세션에서 설치된 `SKILL.md` 경로를 직접 지정할 수 있습니다.

The runner edits the target checkout directly and uses your existing CLI authentication. It does not provide OS isolation or one worktree per worker. Raw run artifacts can contain project source and prompts; keep them private. Scope checks detect some out-of-scope changes after execution rather than acting as a filesystem security boundary. Committing and publishing are separate, explicitly requested actions.

The generic GitHub skill-folder installer is not the installation method for this repository: use `scripts/install_skill.py` so the runtime files are bundled. The source entrypoint also works via `python skills/cli-worker-team/scripts/run_team.py --help` while the complete repository is present.

## Development and evidence

```sh
python -m unittest discover -s tests -q
python benchmarks/validate_project_cases.py
```

These checks require no model calls or credentials. Live scripts under `benchmarks/` invoke billable/quota-consuming CLI sessions when explicitly run. Published JSON contains selected synthetic benchmark evidence, not real project data or authentication files. Full local artifacts linked from the reports are not distributed. Historical commit references are retained to make earlier experiment code inspectable. Recorded model names and measurements describe the tested environment, not guaranteed availability or pricing.

## 한국어 설명

메인이 조사와 구현의 상세 로그를 읽는 대신 **작업을 나누고 짧은 결과를 검토**하도록 만드는 최소 구현입니다. Python 표준 라이브러리만 사용합니다. 서버, DB, 에이전트 프레임워크는 없습니다.

작은 모의 앱의 단독 6회·팀 6회 비교에서 메인 총 토큰은 44.7% 줄었고 양쪽 결과 모두 외부 검사를 통과했습니다. 전체 토큰은 1.79배, 메인 비캐시 입력+출력은 9.2% 늘었으므로 비용 절감이나 대형 프로젝트 성능으로 일반화하지 않습니다. 이 수치는 아래 리뷰 근거 지침 보완 전 결과입니다. [프로젝트 시뮬레이션](SIMULATION.md)과 [이전 비교 결과](BENCHMARK.md)에 조건과 한계를 기록했습니다.

- `worker.py`: Codex CLI 워커 실행·병렬 처리·세션 재개·결과 및 사용량 기록
- `opencode_backend.py`: OpenCode CLI 명령·역할별 권한·JSON 이벤트·최종 보고서 검증·토큰 정규화
- `team.py`: 요청만 보고 위임 → 워커 배치 → 읽기 전용 검토 → 필요하면 같은 워커를 한 번 재개하고 최종 검토. 아주 작은 작업은 첫 턴에서 직접 완료
- `bench.py`: 같은 메인 모델의 단독 실행과 워커 사용 실행 비교

## 준비

Python 3.11 이상, Git, 로그인된 Codex CLI가 필요합니다. 이 구현은 **Codex CLI 0.160.0**의 옵션과 JSONL 형식을 기준으로 합니다. `codex --version`과 `codex login status`로 확인하세요. ChatGPT CLI 인증을 사용하며 별도 API 서버나 API 키는 필요하지 않습니다.

워커 기본값은 `gpt-6.1-sol` / `high`입니다. 실제 사용 가능한 모델은 로그인한 계정에 따릅니다. 메인은 Astra High 또는 Sol 6.1 High로 선택할 수 있습니다.

## 메인에게 작업 맡기기

작업 내용을 UTF-8 파일 `task.txt`에 작성한 뒤 실행합니다. Python이 메인과 워커를 각각 Codex CLI 프로세스로 실행합니다. Windows에서 메인 샌드박스 안에 다시 워커 CLI를 띄울 때 발생한 네트워크 문제를 피하기 위한 구조입니다.

```powershell
python team.py task.txt --cwd C:/path/to/project --main-model gpt-6-astra --worker-model gpt-6.1-sol --output runs/team-01
```

메인을 Sol로 바꾸려면 `--main-model gpt-6.1-sol`로 지정합니다. 양쪽 effort 기본값은 `high`입니다.

### OpenCode 워커 선택

메인은 항상 Codex CLI입니다. 워커의 CLI는 `--worker-backend codex|opencode`로 선택하며 생략하면 기존 Codex 동작을 유지합니다. OpenCode를 선택할 때는 `opencode models`에서 확인한 정확한 `provider/model`을 `--worker-model`로 지정해야 합니다. 아래 모델명은 형식 예시이므로 실제 사용하는 모델로 바꾸세요.

```powershell
python team.py task.txt --cwd C:/path/to/project --worker-backend opencode --worker-model provider/model --output runs/opencode-team-01
```

`--codex`는 메인과 Codex 워커의 실행 파일, `--opencode`는 OpenCode 워커의 실행 파일을 지정합니다. Windows npm 설치는 발견한 shim 옆의 native `opencode.exe`를 사용하며, 찾지 못하면 직접 `.exe` 경로를 지정해야 합니다. 셸을 거치지 않고 프롬프트를 UTF-8 stdin으로 전달하므로 긴 지시문과 경로 공백을 보존합니다.

`--worker-effort`는 OpenCode의 `--variant`로 전달하며 기본값은 `high`입니다. 지원하는 variant는 공급자와 모델에 따라 다릅니다. variant를 지원하지 않는 모델에는 `--worker-effort ""`를 사용해 옵션을 생략합니다. 인증은 각 CLI의 기존 설정을 사용하며 Codex 인증 파일을 OpenCode로 복사하지 않습니다.

OpenCode 역할별 권한은 `OPENCODE_CONFIG_CONTENT`에 실행 중에만 추가합니다. 구현 워커는 파일 편집과 셸 실행이 가능하고, 조사·리뷰 워커는 둘 다 차단합니다. 서브에이전트·스킬 호출은 차단하며 프로젝트 설정 파일은 수정하지 않습니다. OpenCode 도구 권한은 OS 파일시스템 샌드박스가 아니며, 구현 워커의 셸 실행을 작업 폴더에 강하게 격리하지는 않습니다. 기존 실행 후 scope 검사도 그대로 적용합니다.

OpenCode의 `--format json`은 실행 이벤트 형식입니다. 최종 보고서는 프롬프트에 포함된 스키마를 따르도록 요청하고 마지막 완료 메시지의 JSON을 로컬에서 검증합니다. 잘못된 스키마·실행 오류·토큰 한도로 잘린 응답은 실패로 처리하며 자동으로 다른 모델을 사용하지 않습니다. 사용량 누락은 `null`이며 해당 워커는 자동 재수정 대상이 아닙니다. 현재 OpenCode 연동 검증은 모의 CLI 통합 테스트를 포함하며 실제 모델 실행 여부는 별도 검증 결과에 따릅니다.

1. 메인은 위임할 때 파일을 읽거나 명령을 실행하지 않고 요청만으로 작업을 배정합니다. 기본은 조사·구현·테스트를 끝까지 맡는 워커 한 명이며, 요청에서 독립성이 명확한 경우에만 최대 3명으로 나눕니다. 파일 위치가 불명확하면 한 워커에게 `scope=["."]`를 줍니다.
2. Python이 워커 배치를 실행합니다. 실행 대기는 Python이 처리하므로 메인이 로그를 반복 조회할 필요가 없습니다.
3. 같은 메인 세션을 read-only 샌드박스로 재개해 짧은 결과와 실제 관찰한 명령 종료 코드를 전달합니다. 메인은 필요한 코드와 테스트를 읽어 검증합니다. 테스트·빌드 실행과 코드 수정은 워커에게 맡기도록 지시합니다. read-only는 쓰기 제한이며 명령 실행 자체를 차단하는 도구 목록은 아닙니다.
4. 수정이나 추가 검사가 필요하면 메인이 기존 워커 ID와 후속 지시를 반환합니다. Python이 같은 세션·모델·역할·범위를 유지해 해당 워커들을 순서대로 한 번씩 재개합니다. 메인은 다시 읽기 전용으로 검토하고, 문제가 남으면 blocked로 종료합니다.

리뷰는 원래 요구사항을 기준으로 판단합니다. 완료를 막는 지적에는 구체적인 입력·기대 동작과 코드 또는 실행 근거를 요구하고, 런타임에 대한 의심은 기존 피드백 라운드에서 워커에게 재현하도록 합니다. 최종 리뷰에서는 확인된 결함·검증되지 않은 우려·선택적인 개선을 구분합니다. 중요한 검증이 남으면 그 내용을 명시해 blocked로 종료합니다. 이는 모델 지침이며 오탐을 코드로 완전히 차단하는 보장은 아닙니다.

후속 작업은 최대 한 라운드이며 워커마다 한 번입니다. 새로운 워커 생성, 범위 확대, 무한 재시도, 팀 재편성은 하지 않습니다. 중단·실행 오류·사용량 누락·범위 위반이 있는 워커는 자동 재개하지 않습니다. 위치가 알려진 아주 작은 작업은 메인이 첫 턴에 직접 완료하고 `tasks=[]`와 최종 보고서를 반환합니다. 빈 작업 목록만 반환하면 실패로 처리하며 불필요한 두 번째 턴을 호출하지 않습니다. 다음 사용자 작업은 새 `team.py` 실행이며, 작업 사이의 메인 대화 자동 연결은 아직 구현하지 않았습니다. 수동 후속 수정도 아래 `resume` 명령을 사용할 수 있습니다. 자동 재수정이 실행됐다면 `followups/<id>`가 최신 결과입니다.

첫 턴은 직접 완료 경로 때문에 workspace-write 권한입니다. 위임 전 탐색 금지는 프롬프트 정책이며 도구 접근을 강제로 차단하는 기능은 아닙니다. 위임 이후에는 OMP의 director처럼 읽고 검증하고 후속 지시를 보내는 역할을 맡깁니다. 첫 턴 직접 완료는 작은 작업의 비용을 줄이기 위해 유지한 예외입니다. OMP의 상시 비동기 워커 제어·fast/good 자동 모델 선택은 구현하지 않았습니다.

## 워커를 직접 실행하기

작업을 직접 나누고 싶으면 다음 형태의 `tasks.json`을 작성합니다. `team.py`는 이 목록을 메인의 계획에서 자동 생성합니다. `cwd`의 상대 경로는 **tasks.json의 위치**를 기준으로 합니다. `scope`는 cwd 기준의 파일 또는 디렉터리이며 glob은 지원하지 않습니다.

```json
{
  "tasks": [
    {
      "id": "parser",
      "role": "implement",
      "cwd": "C:/path/to/project",
      "scope": ["src/parser.py", "tests/test_parser.py"],
      "prompt": "입력 문자열의 앞뒤 공백을 제거한 뒤 파싱하도록 수정하고 회귀 테스트를 추가하세요.",
      "acceptance": ["기존 입력 동작을 유지한다", "관련 테스트가 통과한다"]
    },
    {
      "id": "review",
      "role": "review",
      "cwd": "C:/path/to/project",
      "scope": ["src/parser.py", "tests/test_parser.py"],
      "prompt": "앞선 구현의 변경과 테스트를 읽고 실제 결함이 있는지 검토하세요. 파일은 수정하지 마세요."
    }
  ]
}
```

```powershell
python worker.py run tasks.json --model gpt-6.1-sol --effort high --output runs/change-01
```

OpenCode 워커를 직접 실행하려면:

```powershell
python worker.py run tasks.json --backend opencode --model provider/model --output runs/opencode-change-01
```

독립적인 scope는 최대 3개까지 동시에 실행합니다. 파일 범위가 겹치고 하나라도 구현 역할이면 입력 순서대로 실행하므로 위 예제의 리뷰는 구현 뒤에 시작합니다. 겹치지 않는 파일이라도 논리적 의존 관계가 있으면 별도 배치로 실행하세요.

`research`, `review`는 read-only, `implement`는 workspace-write 샌드박스로 실행합니다. 역할 선택과 작업 분해는 메인이 담당합니다. Python 실행기는 작업 목록을 스케줄링할 뿐, 추가 모델을 호출해 분류하지 않습니다.

위 샌드박스 이름은 Codex 워커 기준입니다. OpenCode 워커는 앞서 설명한 역할별 도구 권한을 사용합니다.

최대 동시 실행 수와 개별 워커 제한 시간은 `--concurrency 1..3`, `--timeout 600`으로 조절합니다. 실패한 워커가 있어도 다른 워커 결과는 보존하며 전체 실행은 실패 코드로 끝납니다. 출력 폴더는 매번 새 경로를 사용합니다.

## 같은 워커에게 후속 지시

UTF-8 텍스트 파일 `followup.txt`를 작성한 뒤 실행합니다.

```powershell
python worker.py resume runs/change-01/parser followup.txt --output runs/change-02-parser
```

저장한 정확한 세션 ID, 모델, effort, cwd로 재개합니다. 동일한 `CODEX_HOME`에서 실행해야 합니다. 다음 후속 지시는 최신 결과 폴더인 `runs/change-02-parser`를 대상으로 합니다. 오래된 결과를 다시 재개하면 중복 집계를 피하기 위해 거부합니다. 같은 세션을 이 실행기 밖에서 재개하면 사용량 차이에 외부 작업이 섞일 수 있으므로, 측정하는 세션은 실행기로만 이어가세요.

OpenCode 워커도 같은 `resume` 명령을 사용합니다. 저장된 backend·실행 파일·모델·variant·역할을 자동으로 유지하며 `--backend`를 다시 지정하지 않습니다. 처음 실행한 OpenCode 프로필 환경(`HOME`, `USERPROFILE`, `XDG_*`, `OPENCODE_CONFIG*`)을 유지해야 합니다. 프로필 설정 원문은 세션 파일에 저장하지 않고 지문만 기록합니다. 이전 버전에서 생성한 backend 필드 없는 세션은 Codex로 재개합니다.

CLI 프로세스 자체가 시작되지 못한 `launch_failed`는 원본 세션을 소비하지 않습니다. 원인을 해결한 뒤 원본 결과 폴더에서 새 출력 경로로 재시도할 수 있습니다. 실행이 시작된 뒤의 실패는 사용량과 세션 상태가 불확실할 수 있으므로 자동 재시도하지 않습니다.

세션을 유지해도 전체 문맥 비용이 사라지는 것은 아닙니다. 같은 작업의 수정·질의에 재개를 사용하고, 무관한 작업은 새 워커로 시작합니다.

## 결과와 토큰

메인은 표준 출력 또는 배치의 `summary.json`부터 읽습니다. 워커별 폴더에는 다음이 남습니다.

`team.py`가 메인에게 전달하는 배치 보고서는 JSON 전체를 UTF-8 6,000바이트 이내로 제한합니다. 초과하면 상태·짧은 요약·전체 보고서 경로만 전달합니다. 디스크의 원본 보고서는 자르지 않습니다.

| 파일 | 용도 |
|---|---|
| `report.json` | 요약, 상태, 실제 관찰한 변경 파일·실행 명령, 사용량 |
| `changes.patch` | 실행 전후 파일 차이. 바이너리와 일부 개행은 표시용이며 적용 가능한 패치를 보장하지 않음 |
| `session.json` | 재개할 세션 ID와 누적 사용량 |
| `events.jsonl`, `stderr.log` | 문제 발생 시 확인할 원본 로그 |
| `task.json`, `prompt.txt`, `command.json` | 실행 입력과 실제 CLI 인자 |

자동 재수정 기록은 `followups/<id>`, 갱신된 배치 보고서는 `followups/summary.json`, 메인의 마지막 검토는 `review-final`에 남습니다. 메인 사용량은 마지막 누적값, 워커 사용량은 최초 배치와 각 재개분의 차이를 합산하며 중복 계산하지 않습니다.

`worker_claims`는 모델의 자기 보고입니다. 명령 종료 코드는 `observed_commands`, 파일 변경은 `observed_changed_files`와 구분합니다. 워커의 `completed`만으로 코드 품질을 판정하지 마세요.

Codex의 `turn.completed.usage`는 **세션 누적값**입니다. 최초 실행에서는 그대로 사용하고, 재개에서는 이전 누적값을 빼서 이번 호출의 `usage`를 기록합니다. `cumulative_usage`도 별도로 보존합니다. 이 동작과 관련된 [Codex 이슈](https://github.com/openai/codex/issues/16213)를 참고했습니다.

OpenCode의 `step_finish`는 **호출별 단계 사용량**입니다. 모든 단계의 입력(비캐시 + 캐시 읽기 + 캐시 생성)과 출력(일반 출력 + 추론)을 합산하고, 재개분은 이전 값에서 빼지 않습니다. 누적값은 실행기가 직접 합산합니다. 이 정규화는 CLI 1.18.34의 토큰 구조를 기준으로 합니다. OpenCode CLI 이벤트에서 누락되는 내부 호출이나 공급자가 보고하지 않는 사용량까지 완전하게 측정한다는 보장은 없습니다. 기존 벤치마크 스크립트의 격리 프로필·실험은 Codex 전용이며 OpenCode를 대상으로 확장하지 않았습니다.

- 총 토큰 = 입력 + 출력. 캐시 입력은 입력의 일부이고 추론 출력은 출력의 일부이므로 다시 더하지 않습니다.
- 입력, 캐시 입력, 출력, 확인 가능한 추론 출력과 재개 차이를 따로 기록합니다.
- 로그 누락, 알 수 없는 값, 타임아웃은 사용량을 0으로 처리하지 않고 `null`로 남깁니다.
- 이 수치는 **CLI가 보고한 토큰**이며 ChatGPT 요금제 한도 소모율이나 청구 금액이 아닙니다.

## 벤치마크

실행기 자체 테스트에는 모델 호출이 없습니다.

```powershell
python -m unittest discover -s tests -v
```

호스트에서 연결과 결과를 점검하려면:

```powershell
python bench.py --diagnostic --case modules --main-model gpt-6.1-sol
```

각 실행은 새 Git 저장소, 새 세션, 새 Codex 프로필에서 시작합니다. 로그인 정보만 잠시 복사하고 프로필의 스킬·메모리·플러그인·후크는 비활성화합니다. 런타임 프로필은 종료 시 제거됩니다. 프로세스를 강제 종료하면 출력 폴더의 `.runtime-profile-*`이 남을 수 있으므로 인증 파일을 포함한 그 폴더는 공유하지 마세요. 일반 결과 로그에도 작업 코드가 포함될 수 있습니다.

**새 프로필만으로 새 OS 환경이 되지는 않습니다.** 실제 절감 판단은 새 VM/컨테이너에서 동일한 CLI 버전·모델·도구를 설치하고 진행합니다. 설치와 VM 생성은 이 실행기의 범위에 포함하지 않았습니다. 아래 옵션은 실행자가 새 환경임을 명시하는 기록용 표지이며, 자동으로 VM을 만들거나 격리를 검증하지 않습니다.

```powershell
python bench.py --isolated-environment fresh-vm-image-id --case all --repeats 2 --main-model gpt-6-astra --worker-model gpt-6.1-sol
```

3개 과제 × 단독/팀 2개 방식 × 2회 = 12회 메인 실행입니다. 반복마다 A/B 실행 순서를 바꿉니다. 메인을 바꿀 때는 단독/팀 양쪽을 함께 바꿔 새 비교를 만듭니다. 비교 도중 모델을 섞지 않습니다.

과제는 작은 버그 수정, 독립 모듈 2개 구현, 조사 후 중첩 설정 병합 수정입니다. 독립 기능 채점과 생성된 unittest 테스트 실행을 모두 통과해야 합니다. 테스트 없음·실패·전부 skip은 실패이며, 병합은 여러 단계의 중첩과 반환값의 별칭 공유까지 검사합니다. 채점 코드는 작업 프롬프트와 작업 저장소에 넣지 않고 외부에서 실행합니다. 이는 작은 합성 과제 진단이며 공개 벤치마크 점수나 실제 대형 저장소 성능을 대신하지 않습니다. 호스트 진단에서는 에이전트가 바깥 파일을 읽지 못하도록 강하게 격리된 채점기가 아닙니다.

`summary.json`은 메인·워커 토큰과 통과 여부를 분리합니다. 메인 토큰의 중앙값으로 `(단독 - 팀) / 단독`을 계산하되, 실패·미측정·범위 위반이 있거나 양쪽 실행 횟수가 다르면 비교값을 내지 않습니다. 워커 0명을 선택한 정상 실행도 자동 분배 정책의 결과로 포함하며, 실제 위임 횟수는 별도로 표시합니다. 호스트 진단의 값은 `diagnostic_reduction`에만 남고 `isolated_main_reduction`은 항상 `null`입니다. 캐시 효과와 실행 시간도 원본 결과에서 함께 확인하세요. 2회 반복은 예비 측정이며 통계적인 증명이 아닙니다.

배정 정책을 비교하는 고정 실험은 `benchmarks/routing_experiment.py`입니다. 새 환경에서 `python benchmarks/routing_experiment.py --auth-file /path/to/auth.json --isolated-environment fresh-image-id --output bench-runs/routing`으로 실행합니다. 단독·기존 팀(`20fb8cb`)·수정 팀을 같은 모델(Sol 6.1 High)로 비교합니다. 기존 3과제는 방식별 2회씩 순서를 뒤집어 총 18회, 고정한 이 저장소의 이전 버전에 CLI 동시 실행 옵션을 추가하는 과제는 방식별 1회씩 총 3회입니다. 마지막 과제는 대형 저장소 벤치마크가 아닌 보조 사례입니다. 기존 `worker.py`가 달라지면 이 비교 스크립트는 실행을 거부합니다. 현재 워커 재수정 변경 전의 실험이므로 재현 시 `2ee466a` 커밋을 사용합니다. 결과를 보고 프롬프트를 바꿔 같은 실험에 섞지 않습니다.

OMP 방향의 읽기 전용 검토·워커 재수정 비교는 `benchmarks/director_experiment.py`입니다. 단독·이전 팀(`2ee466a`)·현재 팀(`cba3439`)을 같은 과제와 채점기로 비교하며, 이전 팀의 `team.py`와 `worker.py`를 함께 고정합니다. 새 환경에서 `python benchmarks/director_experiment.py --auth-file /path/to/auth.json --isolated-environment fresh-image-id --output bench-runs/director`로 실행합니다. 실행 횟수는 같은 21회이며 크레딧/사용량 한도 오류가 발생하면 즉시 중단합니다. 메인 재개 시 실제 기록된 샌드박스 정책과 워커 재수정 횟수도 보존합니다.

파일·SQLite·서비스·CLI가 연결된 모의 앱으로 개발 작업을 비교하는 실험도 준비했습니다. `benchmarks/project_experiment.py`가 단독·현재 팀을 3과제 × 2회씩 비교합니다. 10월 5일 재실행 12회가 모두 통과했고 메인 토큰은 단독 대비 44.7% 줄었습니다. 전체 토큰은 1.79배이며, 두 워커 자동 분할·병렬 실행도 1회 관찰했습니다. 과제, 채점기 검증, 제외 기록과 재실행 방법은 [SIMULATION.md](SIMULATION.md)에 있습니다.

실제 리뷰·동일 워커 재수정의 효과는 [REVIEW_QUALITY.md](REVIEW_QUALITY.md)에 별도 기록했습니다. 통제한 결함 3개를 모두 발견·수정했지만, 올바른 CSV 동작에 대한 오탐으로 완료를 막은 사례도 1개 있었습니다. 이 실험은 자연 발생 오류율이나 단독 대비 전반적인 품질 우위를 측정한 것은 아닙니다.

## 현재 범위와 제한

- 같은 작업 트리에서 서로 다른 파일을 수정하는 방식입니다. worktree 생성·자동 병합은 없습니다. 메인은 워커 실행 중 같은 파일을 편집하지 않아야 합니다.
- 다른 실행기의 동시 접근은 Git 메타데이터 디렉터리의 `cli-worker.lock`으로 거부합니다. 팀의 계획·워커 실행·최종 검토 전체를 잠그며, 호출자마다 TEMP/HOME이 달라도 같은 잠금을 사용합니다. 강제 종료 후 잠금이 남으면 안내된 파일의 PID가 종료됐는지 확인한 뒤 해당 잠금 파일만 제거하세요.
- `scope`는 스케줄링 규약입니다. 파일별 OS 권한 경계가 아닙니다. 종료 후 Git이 추적하거나 무시하지 않은 일반 파일의 범위 위반을 검사합니다. ignored 파일, 심볼릭 링크, 저장소 밖 변경은 이 검사에 포함하지 않습니다. 병렬 워커 사이의 위반 주체를 확정하지 않습니다.
- OpenCode는 아직 연결하지 않았습니다. Codex 인자 생성과 이벤트 해석을 `build_command` / `parse_events`에 모아두었습니다. OpenCode로 바꿀 때 실제 CLI의 세션·사용량 의미를 검증하고 이 경계를 교체합니다.
- 전역 설정이나 Codex Desktop의 메인 모델을 변경하지 않습니다. 메인 모델은 해당 CLI 세션 또는 벤치마크 옵션에서 선택합니다.

구조 참고: [OMP](https://github.com/can1357/oh-my-pi), [Pi의 subagent 예제](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent/examples/extensions/subagent). 작업 프로세스 분리와 작은 결과 반환 아이디어를 참고했고 소스 코드는 복사하지 않았습니다.
