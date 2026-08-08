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

- Linux 또는 WSL
- Python 3.11 이상
- Git 저장소
- 설치 및 인증된 Codex CLI

별도 Python 패키지 설치는 필요하지 않습니다. 기본 설정은
`.harness/config.toml`에 있습니다.

## 최초 설치

하네스는 일반 Codex 세션과 분리된 전용 `CODEX_HOME`을 사용합니다. 기본 경로는
`${XDG_STATE_HOME:-$HOME/.local/state}/personal-codex-harness/codex-home`이며,
필요하면 절대 경로 환경 변수 `HARNESS_CODEX_HOME`으로 바꿀 수 있습니다.

저장소를 설치하거나 복제한 뒤 다음 스킬을 호출합니다.

```text
$harness-setup
```

스킬은 권한 `0700`의 전용 디렉터리 생성, 전용 Codex 로그인 실행, 바깥 Codex
설정의 `writable_roots` 병합, `doctor` 검증을 순서대로 수행합니다. Codex가 브라우저
인증을 요구하면 사용자는 브라우저에서 인증만 완료하면 됩니다. 인증 파일을 복사하거나
사용자별 절대 경로를 저장소에 커밋하지 않습니다.

새 writable root는 이미 실행 중인 sandbox에 소급 적용될 수 없으므로 스킬이 설정을
처음 변경한 경우 Codex를 한 번 완전히 재시작해야 합니다. 재시작 후 `$harness-setup`을
다시 호출하면 다음 진단까지 자동으로 확인합니다.

```bash
python3 scripts/harness.py doctor
```

경로는 설치 사용자마다 다르므로 저장소에 사용자 이름이나 절대 경로를 커밋하지
않습니다. `$harness-setup`과 `doctor`가 각 설치 환경에 필요한 경로를 직접 계산합니다.
`plan`, `approve`, `run`, `review`도 전용 홈이 쓰기 가능하고
인증 파일이 안전한 권한으로 존재하는지와 그 홈의 `codex login status`가 성공하는지
상태 변경 전에 다시 확인합니다.

## 사용법

일반 사용자는 저장소 스킬 세 개로 전체 흐름을 진행합니다. 하나의 작업 목표가
하나의 `run-id`가 되며, 그 아래에 계획, step 재시도, 검증, 최종 상태와 review가
함께 보존됩니다.

최초 설치 또는 setup 진단 실패 시 먼저 `$harness-setup`을 호출합니다.

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
python3 scripts/harness.py setup
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

바깥 Python controller는 저장소와 위 전용 Codex 상태 경로만 쓸 수 있는 권한으로
실행합니다. 각 내부 Codex 호출은 전용 홈을 프로세스 환경으로 받고 사용자 설정과
규칙을 상속하지 않으며, 추가 writable root를 빈 배열로 덮어씁니다. planner와
reviewer는 `read-only`, executor는 해당 작업 저장소의 `workspace-write`를 유지합니다.
Acceptance Criteria도 Linux의 `codex sandbox :workspace` 안에서 네트워크와 추가
writable root를 끈 채 실행됩니다. 따라서 전용 상태 경로 때문에 전체 하네스 명령에
host 전체 권한을 부여할 필요가 없습니다.

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
| `max_event_log_bytes` | `1000000` | agent JSONL 이벤트 로그별 보존 한도 |
| `max_final_payload_bytes` | `200000` | 구조화된 최종 JSON payload 한도 |
| `max_tool_output_bytes` | `20000` | 개별 command tool 출력 필드의 보존 한도 |
| `max_verification_output_bytes` | `200000` | 검증 명령의 stdout/stderr별 보존 한도 |
| `codex_command` | `"codex"` | 실행할 Codex CLI 명령 이름 |
| `planner.model` | `"gpt-5.6-sol"` | `plan` 호출에 사용할 모델 |
| `planner.reasoning_effort` | `"high"` | `plan` 호출의 reasoning effort |
| `executor.model` | `"gpt-5.6-terra"` | 계획 step 실행에 사용할 모델 |
| `executor.reasoning_effort` | `"xhigh"` | 계획 step 실행의 reasoning effort |
| `reviewer.model` | `"gpt-5.6-sol"` | `review` 호출에 사용할 모델 |
| `reviewer.reasoning_effort` | `"high"` | `review` 호출의 reasoning effort |
| `parallel_readers.enabled` | `true` | plan/review 읽기 전용 subagent 허용 |
| `parallel_readers.max_workers` | `3` | 읽기 전용 subagent 동시 실행 제한 |
| `parallel_readers.model` | `"gpt-5.6-terra"` | reader 모델 |
| `parallel_readers.reasoning_effort` | `"medium"` | reader reasoning effort |
| `parallel_writers.enabled` | `false` | 격리형 병렬 writer 활성화 여부 |
| `parallel_writers.max_workers` | `2` | 병렬 writer 동시 실행 제한 |
| `network.executor_enabled` | `false` | 승인된 step의 executor 네트워크 opt-in을 허용하는 전역 스위치 |

모델 프로필은 역할별 TOML 하위 테이블로 관리합니다. 현재 `planner`와
`executor`, `reviewer`는 각각 `plan`, `run`, `review`에서 사용합니다. 지원되는 reasoning effort는
`minimal`, `low`, `medium`, `high`, `xhigh`입니다.

Executor 네트워크는 2중 opt-in입니다. 전역 설정
`harness.network.executor_enabled = true`와 승인된 plan step의
`network_access: true`가 모두 있어야 해당 step만 네트워크를 사용할 수 있습니다.
어느 한쪽이라도 없으면 오프라인으로 실행됩니다. Planner, reviewer, controller-owned
검증 명령은 항상 오프라인입니다. 이 옵션은 목적지 allowlist가 아니므로, 특정
호스트만 허용하려면 별도의 프록시나 방화벽 정책이 필요합니다.

이벤트 로그 한도는 감사 기록의 저장량만 제한합니다. runner는 한도 이후에도 전체
JSONL 스트림을 읽고 terminal event와 malformed event를 검사하므로, 유효한 최종
payload와 controller 안전 검사가 있으면 독립 검증을 계속합니다. 개별 command
출력은 앞뒤 문맥과 원래 byte 수를 남긴 요약으로 저장합니다. 기존
`max_output_bytes` 설정은 네 한도 모두에 같은 값을 적용하는 마이그레이션 호환
옵션이며 새 분리 설정과 함께 사용할 수 없습니다.

`doctor`는 Codex CLI의 bundled model catalog를 읽어 planner, executor, reviewer와
활성화된 parallel reader 모델이 실제 카탈로그에 있는지도 확인합니다.

## 안전 범위

하네스는 기존 사용자 변경을 자동으로 stage, commit, push, revert 또는 삭제하지
않습니다. 검증 명령은 파괴적인 실행 파일, shell 호출, 변경성 Git 하위 명령 등
알려진 위험 패턴을 계획 검증 시 거부합니다.

검증 명령은 Linux Codex OS sandbox에서 실행되며 네트워크와 저장소 밖 쓰기가
차단됩니다. 전용 `CODEX_HOME` 환경 변수도 실제 검증 명령에 전달하지 않습니다.
내부 agent shell과 검증 명령에서는 하네스 상태 경로 및 OpenAI API key 환경 변수도
제외합니다.
다만 workspace 프로필은 시스템 파일 읽기를 완전히 숨기는 비밀 격리 경계가 아니며,
검증 명령이 저장소 안에서 만든 변경은 실패로 감지할 뿐 자동으로 되돌리지 않습니다.
따라서 승인 전에 모든 검증 argv를 계속 확인해야 합니다.

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
