# Personal Codex Harness

Codex CLI로 코드 작업을 계획하고, 사용자의 승인을 받은 뒤, 순차 실행과 독립
검증까지 수행하는 개인용 Python 하네스입니다. 모델의 완료 보고만 신뢰하지 않고
컨트롤러가 직접 실행한 검증 명령과 Git 변경 증거로 성공 여부를 판단합니다.

## 현재 제공하는 기능

- 읽기 전용 Codex 실행으로 구조화된 초안 계획 생성
- 사용자가 검토한 계획에 대한 명시적 승인
- 승인 시점의 계획 해시와 Git 작업 트리 상태 고정
- 계획 단계를 한 번에 하나씩 실행하고 실패 원인을 전달해 재시도
- 단계별 `allowed_paths` 범위와 Git branch, HEAD, index 변경 감시
- 단계별 Acceptance Criteria와 전체 최종 명령의 독립 실행
- 상태 전이, Codex 이벤트, 검증 stdout/stderr를 실행 기록으로 보존

현재 실행 모델은 단일 planner/executor의 순차 처리이며 병렬 에이전트 실행이나
웹 UI는 포함하지 않습니다.

## 요구 사항

- Python 3.11 이상
- Git 저장소
- 설치 및 인증된 Codex CLI

별도 Python 패키지 설치는 필요하지 않습니다. 기본 설정은
`.harness/config.toml`에 있습니다.

## 사용법

먼저 로컬 환경, 설정, 스키마를 확인합니다.

```bash
python3 scripts/harness.py doctor
```

목표로부터 초안 계획을 만듭니다. 이 단계의 Codex는 저장소를 읽기 전용으로
사용합니다.

```bash
python3 scripts/harness.py plan "<goal>"
```

출력된 `<run-id>`의 `.harness/runs/<run-id>/plan.json`과 `steps/` 문서를
검토합니다. 초안은 승인 전에 직접 수정할 수 있으며 스키마를 만족해야 합니다.
특히 각 단계의 변경 허용 범위와 검증 명령을 확인한 뒤 승인합니다.

```bash
python3 scripts/harness.py approve <run-id>
```

승인 이후 계획 또는 Git 작업 트리가 달라지면 실행은 차단됩니다. 그대로라면
단계를 순서대로 실행하고 각 단계의 검증을 통과한 후 최종 검증을 수행합니다.

```bash
python3 scripts/harness.py run <run-id>
python3 scripts/harness.py status <run-id>
```

`status`와 `run`은 JSON을 출력합니다. 최종 상태는 `completed`, `failed`,
`blocked` 중 하나이며, 차단된 경우 `blocked_reason`과 필요한 조치를 함께
확인할 수 있습니다.

## 실행 방식

`plan`은 Codex의 구조화된 응답을 `schemas/plan.schema.json`과 내부 모델로
검증합니다. `approve`는 그 계획의 해시와 현재 Git fingerprint를 저장합니다.
`run`은 승인 기준이 유지되는 경우에만 각 단계를 `workspace-write`로 실행합니다.

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
    ├── step-00-attempt-01-verification.json
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

## 안전 범위

하네스는 기존 사용자 변경을 자동으로 stage, commit, push, revert 또는 삭제하지
않습니다. 검증 명령은 파괴적인 실행 파일, shell 호출, 변경성 Git 하위 명령 등
알려진 위험 패턴을 계획 검증 시 거부합니다.

검증 명령은 별도 OS sandbox가 아닌 로컬 프로세스로 실행됩니다. 저장소, Git
index, 하네스 기록의 변경은 실패로 감지하지만 저장소 밖에서 발생한 외부 부작용은
감지하거나 되돌릴 수 없습니다. 따라서 승인 전에 모든 검증 명령을 확인해야 합니다.

## 개발 검증

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
PYTHONDONTWRITEBYTECODE=1 python3 -m compileall -q harness scripts/harness.py
```
