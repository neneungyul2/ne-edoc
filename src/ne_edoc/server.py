"""ne-edoc: NE능률 그룹웨어 전자결재·전자문서 조회 MCP 서버 (조회 전용).

실행:
    ne-edoc setup   # ID·비밀번호 저장(윈도우 자격 증명 관리자) + 로그인 확인
    ne-edoc test    # 완결문서 5건 조회 확인
    ne-edoc               # MCP 서버 실행 (Claude Desktop이 실행)
"""
from __future__ import annotations

import csv
import getpass
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from .client import BOXES, EdocClient, EdocError, resolve_box

SERVICE = "ne-edoc"
OLD_SERVICE = "ngw-mcp"  # 이전 이름으로 저장된 계정도 읽는다
DEFAULT_DIR = os.environ.get("NE_EDOC_DATA_DIR") or os.environ.get("NGW_DATA_DIR") or str(Path.home() / "Documents" / "ne-edoc")


# ---------------------------------------------------------------- 계정
def _load_credentials():
    user_id = os.environ.get("NE_EDOC_ID") or os.environ.get("NGW_ID")
    pw = os.environ.get("NE_EDOC_PW") or os.environ.get("NGW_PW")
    try:
        import keyring
        for svc in (SERVICE, OLD_SERVICE):
            uid = user_id or keyring.get_password(svc, "__user_id__")
            if uid and not pw:
                pw = keyring.get_password(svc, uid)
            if uid and pw:
                user_id = uid
                break
    except Exception:
        pass
    if not user_id or not pw:
        raise EdocError("그룹웨어 계정이 설정되지 않았습니다. 명령 프롬프트에서 "
                        "`uvx --from git+<저장소주소> ne-edoc setup` 을 먼저 실행해 주세요.")
    return user_id, pw


_client: EdocClient | None = None


def client() -> EdocClient:
    global _client
    if _client is None:
        uid, pw = _load_credentials()
        _client = EdocClient(uid, pw)
    return _client


def _safe(name: str) -> str:
    import re
    return re.sub(r'[\\/:*?"<>|\r\n]', "_", name).strip()[:80] or "doc"


# ---------------------------------------------------------------- MCP
def build_server():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("ne-edoc", instructions=(
        "NE능률 사내 그룹웨어(ngw.neungyule.com, '그룹웨어'·'전자결재'·'결재'·'문서대장'·'보관함'·"
        "'기안'·'품의서'·'업무협조전'·'비용결의서')를 조회하는 도구다. 사용자가 그룹웨어나 결재 문서를 "
        "말하면 다른 문서 저장소(컨플루언스·드라이브 등)보다 이 도구를 먼저 쓴다. 조회 전용이다."))

    @mcp.tool()
    def edoc_box_counts() -> dict:
        """[NE능률 그룹웨어 전자결재] 전자결재 문서함별 건수(미결·공람 등)를 조회한다."""
        return client().box_counts()

    @mcp.tool()
    def edoc_list_documents(box: str = "ENDLIST", start_date: str | None = None,
                           end_date: str | None = None, keyword: str | None = None,
                           page: int = 1, page_size: int = 50) -> dict:
        """[NE능률 그룹웨어 전자결재] 전자결재 개인 문서함(미결·공람·진행·처리예정·완결·임시저장)의 결재 문서 목록을 조회한다.

        box: 문서함 코드 또는 한글 이름. TODOLIST(미결문서), APP_SHARE(공람문서),
             INGLIST(진행문서), PREVLIST(처리예정함), ENDLIST(완결문서), APP_TEMP(임시저장문서)
        start_date/end_date: 'YYYY-MM-DD'. 비우면 그룹웨어 기본 기간(보통 최근 1년).
        keyword: 제목 검색어. page_size: 최대 100 권장.
        각 문서의 instance_id·ref_id 로 edoc_get_document 를 호출해 본문을 본다.
        """
        return client().list_documents(box, start_date, end_date, keyword, page, min(page_size, 200))

    @mcp.tool()
    def edoc_list_archive_boxes() -> list:
        """[NE능률 그룹웨어 전자결재] 문서대장에서 접근 권한이 있는 부서 보관함 목록(부서명·box_id)을 조회한다."""
        return client().list_archive_boxes()

    @mcp.tool()
    def edoc_list_archive_documents(dept: str | None = None, box_id: str | None = None,
                                   start_date: str | None = None, end_date: str | None = None,
                                   keyword: str | None = None, page: int = 1,
                                   page_size: int = 50) -> dict:
        """[NE능률 그룹웨어 전자결재] 문서대장의 부서 보관함(팀 보관함·완료함) 결재 문서 목록을 조회한다. (예: dept='영업전략팀')

        dept: 부서명 일부(공백 무시). 또는 box_id 를 직접 지정.
              둘 다 비우거나 dept='전체' 면 완료함(접근 가능한 보관함 전체)을 조회한다.
        본문은 edoc_get_document(instance_id, ref_id, box='ENDDOCLIST') 로 본다.
        """
        return client().list_archive_documents(dept, box_id, start_date, end_date, keyword,
                                               page, min(page_size, 200))

    @mcp.tool()
    def edoc_get_document(instance_id: str, ref_id: str, box: str = "ENDLIST",
                         include_html: bool = False) -> dict:
        """[NE능률 그룹웨어 전자결재] 전자결재 문서 하나의 본문·결재선·결재의견·첨부파일 목록을 조회한다.

        instance_id, ref_id 는 목록 결과에서 가져온다.
        box: 목록을 조회한 문서함. 부서 보관함 문서는 'ENDDOCLIST'.
        include_html=True 면 표 구조를 살린 본문 HTML도 함께 준다.
        """
        return client().get_document(instance_id, ref_id, box, include_html=include_html)

    @mcp.tool()
    def edoc_download_attachment(file_id: str, file_name: str | None = None,
                                save_dir: str | None = None) -> dict:
        """[NE능률 그룹웨어 전자결재] 결재 문서의 첨부파일을 PC에 저장하고 저장 경로를 돌려준다.

        file_id 는 edoc_get_document 의 attachments 에서 가져온다.
        save_dir 를 비우면 기본 폴더(문서\\ne-edoc\\attachments)에 저장한다.
        """
        d = save_dir or str(Path(DEFAULT_DIR) / "attachments")
        path = client().download_attachment(file_id, d, file_name)
        return {"saved_path": path, "size": Path(path).stat().st_size}

    @mcp.tool()
    def edoc_export(box: str = "ENDLIST", start_date: str | None = None, end_date: str | None = None,
                   keyword: str | None = None, out_dir: str | None = None,
                   include_attachments: bool = False, max_docs: int = 300,
                   dept: str | None = None) -> dict:
        """[NE능률 그룹웨어 전자결재] 기간 내 결재 문서를 한꺼번에 받아 DB용 파일(CSV·JSONL)로 저장한다.

        out_dir 에 documents.jsonl(문서별 전체 정보)과 documents.csv(요약 표)를 만든다.
        이미 받은 문서는 건너뛰므로 같은 폴더로 다시 실행하면 이어받기가 된다.
        include_attachments=True 면 첨부파일도 attachments/<문서번호>/ 에 저장한다.
        문서대장 기준으로 받으려면 dept 에 부서명(예: '물류파트') 또는 '전체'를 넣는다. (box 는 무시)
        """
        return export(box, start_date, end_date, keyword, out_dir, include_attachments, max_docs, dept)

    return mcp


# ---------------------------------------------------------------- 일괄 저장
CSV_COLS = ["doc_no", "title", "form", "drafter", "status", "date", "box", "attachment_count",
            "attachment_names", "approvers", "opinion_count", "ref_id", "instance_id", "fetched_at"]


def export(box="ENDLIST", start_date=None, end_date=None, keyword=None, out_dir=None,
           include_attachments=False, max_docs=300, dept=None) -> dict:
    c = client()
    if dept:
        box = "ENDDOCLIST"
        if dept.strip() in ("전체", "완료함", "all"):
            lister = lambda pg: c.list_archive_documents("전체", None, start_date, end_date, keyword, pg, 50)
            tag = "문서대장_전체"
        else:
            b = c._find_box(dept, None)
            lister = lambda pg: c.list_archive_documents(None, b["box_id"], start_date, end_date, keyword, pg, 50)
            tag = "보관함_" + _safe(b["dept"] or dept)
    else:
        box = resolve_box(box)
        lister = lambda pg: c.list_documents(box, start_date, end_date, keyword, pg, 50)
        tag = box
    out = Path(out_dir or Path(DEFAULT_DIR) / f"export_{tag}")
    out.mkdir(parents=True, exist_ok=True)
    jl, cf = out / "documents.jsonl", out / "documents.csv"
    done = set()
    if jl.exists():
        for line in jl.read_text(encoding="utf-8").splitlines():
            try:
                done.add(json.loads(line)["ref_id"])
            except Exception:
                pass
    new_csv = not cf.exists()
    fetched, skipped, errors, page = 0, 0, [], 1
    with open(jl, "a", encoding="utf-8") as fj, open(cf, "a", encoding="utf-8-sig", newline="") as fc:
        w = csv.DictWriter(fc, fieldnames=CSV_COLS)
        if new_csv:
            w.writeheader()
        while fetched < max_docs:
            lst = lister(page)
            rows = lst["documents"]
            if not rows:
                break
            for row in rows:
                if fetched >= max_docs:
                    break
                if row["ref_id"] in done:
                    skipped += 1
                    continue
                try:
                    doc = c.get_document(row["instance_id"], row["ref_id"], box)
                except EdocError as e:
                    errors.append({"ref_id": row["ref_id"], "title": row.get("제목"), "error": str(e)})
                    continue
                doc["list_row"] = row
                doc["fetched_at"] = datetime.now().isoformat(timespec="seconds")
                if include_attachments and doc["attachments"]:
                    d = out / "attachments" / _safe(doc["doc_no"] or doc["ref_id"])
                    for a in doc["attachments"]:
                        try:
                            a["local_path"] = c.download_attachment(a["file_id"], str(d), a["name"])
                        except EdocError as e:
                            a["download_error"] = str(e)
                fj.write(json.dumps(doc, ensure_ascii=False) + "\n")
                fj.flush()
                steps = [s for g in doc.get("approval_line", []) for s in g["steps"]]
                w.writerow({
                    "doc_no": doc["doc_no"], "title": doc["title"], "form": row.get("양식", "") or doc.get("form_alias", ""),
                    "drafter": row.get("기안자", ""), "status": row.get("상태", ""),
                    "date": row.get("결재완료일") or row.get("기안일") or row.get("수신일") or "",
                    "box": box, "attachment_count": len(doc["attachments"]),
                    "attachment_names": " | ".join(a["name"] for a in doc["attachments"]),
                    "approvers": " > ".join(f"{s['name']}({s['role']})" for s in steps),
                    "opinion_count": len(doc.get("opinions", [])),
                    "ref_id": doc["ref_id"], "instance_id": doc["instance_id"],
                    "fetched_at": doc["fetched_at"]})
                fc.flush()
                done.add(row["ref_id"])
                fetched += 1
            total = lst.get("total")
            if (total is not None and page * 50 >= total) or (total is None and len(rows) < 50):
                break
            page += 1
    return {"out_dir": str(out), "fetched": fetched, "skipped_existing": skipped,
            "errors": errors[:20], "error_count": len(errors),
            "files": [str(jl), str(cf)]}


# ---------------------------------------------------------------- CLI
def _setup():
    import keyring
    uid = input("그룹웨어 ID: ").strip()
    pw = getpass.getpass("그룹웨어 비밀번호(화면에 표시되지 않음): ")
    print("로그인 확인 중...")
    EdocClient(uid, pw).login()
    keyring.set_password(SERVICE, "__user_id__", uid)
    keyring.set_password(SERVICE, uid, pw)
    print("로그인 성공. 윈도우 자격 증명 관리자에 저장했습니다.")


def _diag():
    """로그인 과정을 점검한다. 비밀번호는 출력하지 않는다."""
    uid = input("그룹웨어 ID: ").strip()
    pw = getpass.getpass("그룹웨어 비밀번호(화면에 표시되지 않음): ")
    c = EdocClient(uid, pw)
    try:
        c.login()
        print("로그인 성공")
    except EdocError as e:
        print("로그인 실패:", e)
    dbg = getattr(c, "last_login_debug", {})
    print("\n[점검 결과] 아래 내용을 그대로 복사해 Claude에게 붙여 주세요.")
    print("POST 상태코드:", dbg.get("post_status"))
    print("리다이렉트:", [u.split("?")[0] + (" (?쿼리 있음)" if "?" in u else "") for u in dbg.get("redirects", [])])
    print("최종 주소:", dbg.get("final_url"))
    print("쿠키 이름:", dbg.get("cookies"))
    print("로그인 화면과 다른 줄(최대 40줄):")
    for ln in dbg.get("new_lines", []):
        print("   ", ln[:200])


def _test():
    r = client().list_documents("ENDLIST", page_size=5)
    print(f"완결문서 총 {r['total']}건 (조회기간 {r['search_period']})")
    for d in r["documents"]:
        print("-", d.get("문서번호"), "|", d.get("제목"), "| 첨부" if d["has_attachment"] else "")
    if r["documents"]:
        d = r["documents"][0]
        doc = client().get_document(d["instance_id"], d["ref_id"])
        print("\n[첫 문서 본문 앞부분]\n", doc["body_text"][:300])
        print("첨부:", [a["name"] for a in doc["attachments"]])
        print("결재선:", [s["name"] for g in doc.get("approval_line", []) for s in g["steps"]])


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    try:
        if cmd == "setup":
            _setup()
        elif cmd == "diag":
            _diag()
        elif cmd == "test":
            _test()
        elif cmd in ("serve", "run"):
            build_server().run()
        else:
            print("사용법: ne-edoc [setup | test | diag]  (인자 없이 실행하면 MCP 서버)")
    except EdocError as e:
        print("오류:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
