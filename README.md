# Personal Codex Harness

Codex CLI로 코드 작업을 계획하고, 사용자의 승인을 받은 뒤, 실행과 독립
검증·리뷰까지 수행하는 개인용 Python 하네스입니다. 모델의 완료 보고만 신뢰하지 않고
컨트롤러가 직접 실행한 검증 명령과 Git 변경 증거로 성공 여부를 판단합니다.

## 현재 제공하는 기능

- 읽기 전용 Codex 실행으로 구조화된 초안 계획 생성
- 사용자가 검토한 계획에 대한 명시적 승인
- 승인 시점의 계획 해시와 Git 작업 트리 상태 고정
- 역할별 모델과 reasoning effort 설정
- 계획·리뷰에서 제한된 읽기 전용 병렬 subagent 사용
- 기본 순차 실행과 선택적 격리형 병렬 writer
- 단계별 `allowed_paths` 범위와 Git branch, HEAD, index 변경 감시
- 단계별 Acceptance Criteria와 전체 최종 명령의 독립 실행
- controller 상태를 바꾸지 않는 독립 `review` 명령
- 자동 갱신되는 localhost 진행 대시보드
- 상태 전이, Codex 이벤트, 검증 stdout/stderr를 실행 기록으로 보존

## 요구 사항

- Python 3.11 이상
- Git 저장소
- 설치 및 인증된 Codex CLI

별도 Python 패키지 설치는 필요하지 않습니다. 기본 설정은
`.harness/config.toml`에 있습니다.

## 사용법

일반 사용자는 저장소 스킬 두 개로 전체 흐름을 진행합니다. 하나의 작업 목표가
하나의 `run-id`가 되며, 그 아래에 계획, step 재시도, 검증, 최종 상태와 review가
함께 보존됩니다.

```text
$harness-plan "youtube URL 영상 내용 분석 프로그램 만들어줘"
```

이 스킬은 `doctor` 사전점검 후 읽기 전용 계획을 만들고 해당 run의 localhost UI를
백그라운드로 시작해 기본 브라우저에서 엽니다. 브라우저 실행을 지원하지 않는 환경이면
접속 URL을 반환합니다. UI에서 계획과 `allowed_paths`, Acceptance Criteria를 확인하고
필요하면 승인 전에 `plan.json`을 수정합니다.

```text
$harness-approve <run-id>
```

명시적으로 이 스킬을 호출하면 정확히 그 run을 승인한 뒤 `run`을 실행하고, 결과가
`completed`, `failed`, `blocked` 중 하나에 도달하면 독립 `review`까지 자동으로
수행합니다. UI는 전체 과정을 계속 갱신하며 백그라운드 UI 프로세스를 중지할 때까지
유지됩니다.

승인 이후 계획 또는 Git 작업 트리가 달라지면 실행은 차단됩니다. review는 자문
보고서만 저장하며 controller의 최종 상태를 바꾸지 않습니다.

### 내부 CLI

스킬은 다음 CLI를 순서대로 호출합니다. 자동화, 진단 또는 복구 시 직접 사용할 수
있습니다.

```bash
python3 scripts/harness.py doctor
python3 scripts/harness.py plan "<goal>"
python3 scripts/harness.py approve <run-id>
python3 scripts/harness.py run <run-id>
python3 scripts/harness.py status <run-id>
python3 scripts/harness.py review <run-id>
python3 scripts/harness.py ui <run-id> --open-browser
```

`status`와 `run`은 JSON을 출력합니다. 차단된 경우 `blocked_reason`과 필요한 조치를
함께 확인할 수 있습니다. UI는 읽기 전용 snapshot을 750ms마다 갱신합니다.

## 실행 방식

`plan`은 Codex의 구조화된 응답을 `schemas/plan.schema.json`과 내부 모델로
검증합니다. `approve`는 그 계획의 해시와 현재 Git fingerprint를 저장합니다.
`run`은 승인 기준이 유지되는 경우에만 각 단계를 `workspace-write`로 실행합니다.
기본값은 기존과 같은 순차 실행입니다. 병렬 writer를 명시적으로 활성화하면
`depends_on`이 충족되고 `allowed_paths`가 겹치지 않는 step을 임시 저장소 복제본에서
동시에 실행합니다. 모든 worker가 독립 검증을 통과한 batch만 실제 작업 트리에
통합하고 각 step 검증을 다시 실행합니다.

Codex가 완료를 보고해도 컨트롤러는 계획에 적힌 Acceptance Criteria 명령을
직접 다시 실행합니다. 명령은 shell 없이 argv 배열로 실행되며 하나라도 실패하면
해당 단계가 재시도됩니다. 총 시도 횟수는 기본 3회입니다. 모든 단계가 성공한
뒤에는 `final_acceptance_commands`를 한 번 더 실행해야 전체 run이 완료됩니다.

실행 중 다음 변경이 감지되면 완료로 처리하지 않습니다.

- 단계의 `allowed_paths` 밖에 있는 파일 변경
- Git branch, HEAD 또는 index 변경
- 컨트롤러가 관리하는 run 상태나 기존 증거 변경
- 검증 명령에 의한 저장소 또는 Git 상태 변경

## 실행 기록

실행별 기록은 Git에서 제외된 `.harness/runs/<run-id>/`에 저장됩니다.

```text
.harness/runs/<run-id>/
├── request.md
├── plan.json
├── state.json
├── approved-git.json
├── events.jsonl
├── steps/
│   └── 00-<step-name>.md
└── evidence/
    ├── plan-events.jsonl
    ├── step-00-attempt-01.jsonl
    ├── step-00-attempt-01-agent.json
    ├── step-00-attempt-01-isolated-verification.json
    ├── step-00-attempt-01-verification.json
    ├── review-01-events.jsonl
    ├── review-01.json
    └── final-verification.json
```

## 설정

`.harness/config.toml`에서 다음 값을 조정할 수 있습니다.

| 항목 | 기본값 | 설명 |
| --- | ---: | --- |
| `max_retries` | `3` | 최초 실행을 포함한 단계별 최대 시도 횟수 |
| `timeout_seconds` | `1800` | Codex 실행 제한 시간(초) |
| `verification_timeout_seconds` | `900` | 검증 명령별 제한 시간(초) |
| `max_output_bytes` | `200000` | 프로세스별 보존 출력의 최대 크기 |
| `codex_command` | `"codex"` | 실행할 Codex CLI 명령 이름 |
| `planner.model` | `"gpt-5.6-sol"` | `plan` 호출에 사용할 모델 |
| `planner.reasoning_effort` | `"high"` | `plan` 호출의 reasoning effort |
| `executor.model` | `"gpt-5.6-luna"` | 계획 step 실행에 사용할 모델 |
| `executor.reasoning_effort` | `"xhigh"` | 계획 step 실행의 reasoning effort |
| `reviewer.model` | `"gpt-5.6-sol"` | `review` 호출에 사용할 모델 |
| `reviewer.reasoning_effort` | `"high"` | `review` 호출의 reasoning effort |
| `parallel_readers.enabled` | `true` | plan/review 읽기 전용 subagent 허용 |
| `parallel_readers.max_workers` | `3` | 읽기 전용 subagent 동시 실행 제한 |
| `parallel_readers.model` | `"gpt-5.6-luna"` | reader 모델 |
| `parallel_readers.reasoning_effort` | `"medium"` | reader reasoning effort |
| `parallel_writers.enabled` | `false` | 격리형 병렬 writer 활성화 여부 |
| `parallel_writers.max_workers` | `2` | 병렬 writer 동시 실행 제한 |

모델 프로필은 역할별 TOML 하위 테이블로 관리합니다. 현재 `planner`와
`executor`, `reviewer`는 각각 `plan`, `run`, `review`에서 사용합니다. 지원되는 reasoning effort는
`minimal`, `low`, `medium`, `high`, `xhigh`입니다.

## 안전 범위

하네스는 기존 사용자 변경을 자동으로 stage, commit, push, revert 또는 삭제하지
않습니다. 검증 명령은 파괴적인 실행 파일, shell 호출, 변경성 Git 하위 명령 등
알려진 위험 패턴을 계획 검증 시 거부합니다.

검증 명령은 별도 OS sandbox가 아닌 로컬 프로세스로 실행됩니다. 저장소, Git
index, 하네스 기록의 변경은 실패로 감지하지만 저장소 밖에서 발생한 외부 부작용은
감지하거나 되돌릴 수 없습니다. 따라서 승인 전에 모든 검증 명령을 확인해야 합니다.

병렬 writer는 실제 `.git`이나 controller run metadata를 worker에게 노출하지 않고
임시 복제본을 사용합니다. 첫 버전은 파일 추가·수정만 통합하며 삭제와 symlink
결과는 거부합니다. UI는 `127.0.0.1`에만 열리고 읽기 전용 GET API만 제공합니다.

## 개발 검증

핵심 코드는 책임별로 `domain`, `orchestration`, `agents`, `safety`, `storage`,
`ui` 패키지에 나뉩니다. `HarnessController`만 run/step 상태를 변경하고 다른
패키지는 검증된 값, 실행 결과 또는 읽기 전용 정보를 반환합니다.
저장소 전체에 적용되는 권한과 패키지 경계는 루트의 `HARNESS_DESIGN.md`에
정리되어 있습니다.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q harness scripts/harness.py
```
