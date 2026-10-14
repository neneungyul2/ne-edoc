# ne-edoc

Claude가 NE능률 그룹웨어의 **전자결재·전자문서를 조회**할 수 있게 해 주는 도구(MCP 서버)입니다.
결재·삭제·저장은 하지 않습니다. 각자 본인 계정으로 로그인하므로 **본인이 그룹웨어에서 볼 수 있는 범위만** 조회됩니다.
사내망에서만 동작합니다.

## 할 수 있는 일

| 도구 | 하는 일 |
|---|---|
| `edoc_box_counts` | 문서함별 건수 |
| `edoc_list_documents` | 개인 문서함 목록 (미결·공람·진행·처리예정·완결·임시저장, 기간·검색어) |
| `edoc_list_archive_boxes` | 문서대장에서 접근 가능한 부서 보관함 목록 |
| `edoc_list_archive_documents` | 문서대장 보관함 문서 목록 (부서명으로 지정, 비우면 완료함 전체) |
| `edoc_get_document` | 문서 본문, 결재선, 결재의견, 첨부파일 목록 |
| `edoc_download_attachment` | 첨부파일을 PC에 저장 |
| `edoc_export` | 기간 내 문서를 한꺼번에 `documents.csv`(요약)·`documents.jsonl`(전체)로 저장. 첨부 포함 가능. 다시 실행하면 이어받기 |

## 설치 (처음 한 번)

준비물: 회사 PC, Claude Desktop, GitHub 저장소 접근 권한

1. **uv 설치** (PowerShell)
   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```
   설치 후 PowerShell 창을 새로 엽니다.

2. **계정 저장** — 그룹웨어 ID·비밀번호를 입력합니다. 비밀번호는 윈도우 자격 증명 관리자에 저장되고 파일에 남지 않습니다.
   ```powershell
   uvx --from git+https://github.com/<계정>/ne-edoc ne-edoc setup
   ```

3. **동작 확인** — 완결문서 5건이 나오면 정상입니다.
   ```powershell
   uvx --from git+https://github.com/<계정>/ne-edoc ne-edoc test
   ```
   로그인이 안 되면 `test` 대신 `diag` 를 실행하고 결과를 공유해 주세요.

4. **Claude Desktop 연결** — `%APPDATA%\Claude\claude_desktop_config.json` 의 `mcpServers` 안에 추가합니다.
   ```json
   "ne-edoc": {
     "command": "uvx",
     "args": ["--refresh", "--from", "git+https://github.com/<계정>/ne-edoc", "ne-edoc"]
   }
   ```
   `--refresh` 때문에 Claude Desktop을 켤 때마다 최신 버전을 받아 실행합니다.

5. Claude Desktop을 트레이 아이콘에서 완전히 종료한 뒤 다시 켭니다.

## 이렇게 물어보면 됩니다

- "그룹웨어 완결문서 최근 10건 보여줘"
- "그룹웨어 공람문서 전체 기간으로 보여줘"
- "그룹웨어 문서대장에서 우리 팀 보관함 이번 달 문서 찾아줘"
- "이 문서 본문이랑 결재선 보여주고, 첨부파일 받아줘"
- "그룹웨어 완결문서 9~10월분을 첨부 포함해서 한꺼번에 저장해줘"

조회 기간을 말하지 않으면 그룹웨어 기본값(최근 1년)으로 조회합니다.

## 선택 설정 (환경변수)

| 이름 | 뜻 | 기본값 |
|---|---|---|
| `NE_EDOC_DATA_DIR` | 첨부·일괄저장 기본 폴더 | `문서\ne-edoc` |

## 주의

- 이 도구는 브라우저와 따로 로그인합니다. 브라우저의 그룹웨어 로그인이 끊길 수 있습니다.
- 그룹웨어 비밀번호를 바꾸면 `setup` 을 다시 실행합니다.
- 그룹웨어 화면이 개편되면 조회가 안 될 수 있습니다.
- 요청 사이에 0.3초씩 쉽니다. 대량 저장은 `max_docs` 로 나눠 받으세요.

## 구조

- `src/ne_edoc/server.py` : MCP 서버, `setup`·`test`·`diag` 명령
- `src/ne_edoc/client.py` : 그룹웨어 요청·화면 해석
- `src/ne_edoc/xcrypt.py` : 로그인 때 그룹웨어가 쓰는 암호화를 그대로 옮긴 것
