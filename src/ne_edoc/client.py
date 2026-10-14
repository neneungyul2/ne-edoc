"""NE능률 그룹웨어(XClick R3) 전자결재·전자문서 조회 클라이언트.

조회 전용이다. 결재·삭제·저장 등 상태를 바꾸는 요청은 보내지 않는다.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

from .xcrypt import encrypt

BASE = os.environ.get("NE_EDOC_BASE") or os.environ.get("NGW_BASE") or "https://ngw.neungyule.com/xclickr3_ne"
CTRL = BASE + "/XClickController"

# 문서함 코드 → (facade, command, nextpage, 표시이름)
BOXES = {
    "TODOLIST": ("AppListFacade", "listTodo", "/app/appTodoList.jsp", "미결문서"),
    "APP_SHARE": ("AppListFacade", "listAppShare", "/app/appShareList.jsp", "공람문서"),
    "INGLIST": ("AppListFacade", "listIng", "/app/appIngList.jsp", "진행문서"),
    "PREVLIST": ("AppListFacade", "listAppPrev", "/app/appPrevList.jsp", "처리예정함"),
    "ENDLIST": ("AppListFacade", "listAppEnd", "/app/appEndList.jsp", "완결문서"),
    "APP_TEMP": ("AppListFacade", "listMyTempSaveApp", "/app/appTempSaveList.jsp", "임시저장문서"),
}
BOX_ALIASES = {v[3]: k for k, v in BOXES.items()}
BOX_ALIASES.update({"미결": "TODOLIST", "공람": "APP_SHARE", "진행": "INGLIST",
                    "처리예정": "PREVLIST", "완결": "ENDLIST", "임시저장": "APP_TEMP"})


class EdocError(Exception):
    pass


def _clean(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


_BLOCK = {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section",
          "article", "header", "footer", "blockquote", "pre", "hr", "dl", "dt", "dd"}


def html_to_text(node) -> str:
    """글자 단위로 쪼개진 span 을 붙이고, 표는 '칸 | 칸' 형태로 바꿔 읽기 좋은 글로 만든다."""
    from bs4 import NavigableString, Tag, Comment, Doctype, Declaration, ProcessingInstruction

    def walk(n) -> str:
        if isinstance(n, (Comment, Doctype, Declaration, ProcessingInstruction)):
            return ""
        if isinstance(n, NavigableString):
            return str(n).replace("\r", "").replace("\n", " ")
        if not isinstance(n, Tag):
            return ""
        name = n.name.lower()
        if name in ("script", "style", "meta", "head", "title", "input", "button", "select"):
            return ""
        if name == "br":
            return "\n"
        if name == "table":
            rows = []
            for tr in n.find_all("tr"):
                if tr.find_parent("table") is not n:
                    continue
                cells = []
                for td in tr.find_all(["td", "th"], recursive=False):
                    t = re.sub(r"[ \t\xa0]+", " ", walk_children(td))
                    t = re.sub(r" *\n *", "\n", t)
                    t = re.sub(r"\n{2,}", "\n", t).strip()
                    cells.append(t)
                while cells and not cells[-1]:
                    cells.pop()
                if any(cells):
                    rows.append(" | ".join(cells))
            return "\n" + "\n".join(rows) + "\n"
        inner = walk_children(n)
        if name in _BLOCK:
            return "\n" + inner + "\n"
        return inner

    def walk_children(n) -> str:
        return "".join(walk(c) for c in n.children)

    text = walk(node)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _norm_date(v: str, today=None):
    """그룹웨어 목록의 날짜 표기(14:02 / 10-13 11:40 / 2025-11-03)를 YYYY-MM-DD[ HH:MM] 로 바꾼다."""
    from datetime import date
    v = (v or "").strip()
    today = today or date.today()
    if re.fullmatch(r"\d{1,2}:\d{2}", v):
        return f"{today:%Y-%m-%d} {v}"
    m = re.fullmatch(r"(\d{2})-(\d{2})(?: (\d{1,2}:\d{2}))?", v)
    if m:
        y = today.year if (int(m.group(1)), int(m.group(2))) <= (today.month, today.day) else today.year - 1
        return f"{y}-{m.group(1)}-{m.group(2)}" + (f" {m.group(3)}" if m.group(3) else "")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}( \d{1,2}:\d{2})?", v):
        return v
    return v


def _jquery_param(pairs) -> str:
    """jQuery.param 과 같은 방식으로 인코딩한다."""
    enc = lambda s: quote(str(s), safe="-_.!~*'()")
    return "&".join(f"{enc(k)}={enc(v)}" for k, v in pairs).replace("%20", "+")


ARCHIVE = "ENDDOCLIST"  # 문서대장 > 부서 보관함


def resolve_box(box: str) -> str:
    b = (box or "ENDLIST").strip()
    if b.upper() == ARCHIVE or b in ("보관함", "문서대장"):
        return ARCHIVE
    if b.upper() in BOXES:
        return b.upper()
    if b in BOX_ALIASES:
        return BOX_ALIASES[b]
    raise EdocError(f"알 수 없는 문서함: {box}. 사용 가능: {', '.join(BOXES)} 또는 "
                   + ", ".join(v[3] for v in BOXES.values()))


class EdocClient:
    def __init__(self, user_id: str, password: str, min_interval: float = 0.3):
        self.user_id = user_id
        self._password = password
        self.min_interval = min_interval
        self._last = 0.0
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
            "Accept-Language": "ko-KR,ko;q=0.9",
        })
        self.logged_in = False
        self.last_login_debug = {}

    # ------------------------------------------------------------ 기본 요청
    def _throttle(self):
        wait = self.min_interval - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()

    @staticmethod
    def _text(r: requests.Response) -> str:
        enc = r.encoding if r.encoding and "charset" in r.headers.get("Content-Type", "").lower() else "utf-8"
        return r.content.decode(enc, errors="replace")

    @staticmethod
    def _is_logged_out(html: str) -> bool:
        return len(html) < 3000 and "/gate/login.jsp" in html

    def login(self):
        self._throttle()
        r = self.s.get(BASE + "/gate/login.jsp", timeout=30)
        soup = BeautifulSoup(self._text(r), "html.parser")
        form = soup.find("form", attrs={"name": "loginForm"})
        if form is None:
            raise EdocError("로그인 화면 구조가 예상과 다릅니다. (loginForm 없음)")
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if name and inp.get("type", "text") == "hidden":
                data[name] = inp.get("value", "")
        data["screenWidth"] = data.get("screenWidth") or "1920"
        data["_xcsparam"] = encrypt(_jquery_param([("id", self.user_id), ("passwd", self._password)]))
        data["_clientId"] = ""
        action = form.get("action") or "/xclickr3_ne/XClickLoginHandler"
        url = action if action.startswith("http") else BASE.split("/xclickr3_ne")[0] + action
        login_page = self._text(r)
        origin = BASE.split("/xclickr3_ne")[0]
        self._throttle()
        r = self.s.post(url, data=data, timeout=30, allow_redirects=True,
                        headers={"Referer": BASE + "/gate/login.jsp", "Origin": origin})
        resp = self._text(r)
        self.last_login_debug = {
            "post_status": r.status_code,
            "redirects": [h.headers.get("Location", "") for h in r.history],
            "final_url": r.url.split("?")[0],
            "cookies": sorted({c.name for c in self.s.cookies}),
            "new_lines": [ln.strip() for ln in resp.splitlines()
                          if ln.strip() and ln.strip() not in login_page][:40],
        }
        if "openDupSelectPop" in resp:
            raise EdocError("중복 로그인 확인 화면이 떴습니다. 다른 곳(브라우저 등)에서 같은 계정이 로그인 중일 수 "
                           "있습니다. 그룹웨어 설정 또는 IT 담당과 중복 로그인 허용 여부를 확인해 주세요.")
        if "openEditPasswdPop" in resp and "passwd" in resp and "changeType" in resp:
            raise EdocError("비밀번호 변경 요청 화면이 떴습니다. 브라우저에서 비밀번호를 변경한 뒤 다시 설정해 주세요.")
        # 로그인 성공 여부 확인
        self._throttle()
        chk = self._text(self.s.get(CTRL, params={"nextpage": "/main.jsp"}, timeout=30))
        if self._is_logged_out(chk):
            new = " ".join(self.last_login_debug["new_lines"])
            msgs = re.findall(r"[가-힣][가-힣0-9 .,()!?·\-]{4,}", new)
            hint = " / ".join(dict.fromkeys(msgs))[:300]
            raise EdocError("로그인에 실패했습니다." + (f" (서버 메시지: {hint})" if hint else "")
                           + " 자세한 내용은 `ne-edoc diag` 로 확인하세요.")
        self.logged_in = True

    def _call(self, method: str, url: str, **kw) -> str:
        if not self.logged_in:
            self.login()
        for attempt in range(2):
            self._throttle()
            r = self.s.request(method, url, timeout=60, **kw)
            html = self._text(r)
            if not self._is_logged_out(html):
                return html
            self.logged_in = False
            self.login()
        raise EdocError("세션이 계속 끊깁니다. 잠시 후 다시 시도해 주세요.")

    # ------------------------------------------------------------ 건수
    def box_counts(self) -> dict:
        txt = self._call("GET", BASE + "/AjaxController",
                         params={"facade": "AppListFacade", "command": "getAppCntAjax",
                                 "ajax_result_type": "JSON"})
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            return {"raw": txt[:2000]}

    # ------------------------------------------------------------ 목록
    def list_documents(self, box="ENDLIST", start_date=None, end_date=None, keyword=None,
                       page=1, page_size=50) -> dict:
        box = resolve_box(box)
        facade, command, nextpage, label = BOXES[box]
        data = {"facade": facade, "command": command, "nextpage": nextpage,
                "leftPaneMenuId": box, "paging_listcnt": str(page_size), "currpage_no": str(page),
                "srchWholeYn": "N", "srchFormId": "ALL", "srchStatus": "ALL"}
        if start_date:
            data["srchStartDate"] = start_date
        if end_date:
            data["srchEndDate"] = end_date
        if keyword:
            data["srchKeyword"] = keyword
        html = self._call("POST", CTRL, data=data)
        out = self._parse_list(html, box)
        out.update({"box": box, "box_name": label, "page": page, "page_size": page_size})
        return out

    def _parse_list(self, html: str, box: str) -> dict:
        soup = BeautifulSoup(html, "html.parser")
        headers = [_clean(th.get_text(" ")) for th in soup.select("thead th")]
        rows = []
        for tr in soup.select("tbody tr"):
            view = tr.select_one('[onclick^="viewApp("]')
            if view is None:
                continue
            args = re.findall(r"'([^']*)'", view.get("onclick", ""))
            tds = tr.find_all("td", recursive=False)
            row = {}
            for i, td in enumerate(tds):
                key = headers[i] if i < len(headers) and headers[i] else None
                if key:
                    row[key] = _clean(td.get_text(" "))
            if any("첨부" in h for h in headers):
                row["has_attachment"] = tr.select_one('[onclick^="viewAppAttachPop"]') is not None
            else:
                row["has_attachment"] = None  # 이 문서함 목록은 첨부 여부를 보여주지 않음
            for k in list(row):
                if k.endswith("일") or k.endswith("일시") or k.endswith("일자"):
                    row[k + "_iso"] = _norm_date(row[k])
            row["instance_id"] = args[1] if len(args) > 1 else ""
            row["ref_id"] = args[5] if len(args) > 5 else ""
            row["box"] = box
            rows.append(row)
        m = re.search(r"총\s*([\d,]+)\s*건", soup.get_text(" "))
        total = int(m.group(1).replace(",", "")) if m else None
        used = {}
        for n in ("srchStartDate", "srchEndDate"):
            el = soup.find("input", attrs={"name": n})
            if el is not None:
                used[n] = el.get("value", "")
        return {"total": total, "search_period": used, "documents": rows}

    # ------------------------------------------------------------ 문서대장(부서 보관함)
    def list_archive_boxes(self) -> list:
        """접근 권한이 있는 부서 보관함 목록."""
        html = self._call("POST", CTRL, data={
            "facade": "AppBoxFacade", "nextpage": "/app/doc/inc/appBoxTreeInc.jsp",
            "transaction_yn": "N", "paging_listcnt": "10", "refreshYn": "N", "menuId": "DOC_APPBOX"})
        pat = re.compile(r"new\s+TreeData\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*"
                         r"decodeURL\('([^']*)'\)\s*,\s*(\d+)")
        nodes = {}
        order = []
        for nid, tid, up, name, depth in pat.findall(html):
            tid, up = tid.strip(), up.strip()
            try:
                name = unquote(name)
            except Exception:
                pass
            nodes[tid] = {"id": nid, "tree_id": tid, "up": up, "name": name, "depth": int(depth)}
            order.append(tid)

        def path(tid):
            names, cur, guard = [], nodes.get(tid), 0
            while cur and guard < 20:
                names.append(cur["name"])
                cur = nodes.get(cur["up"])
                guard += 1
            return list(reversed(names))

        boxes = []
        for tid in order:
            n = nodes[tid]
            if n["id"].startswith(("NE1AP", "MIGBOX")) or n["name"] == "보관함":
                p = path(tid)
                dept = (p[-2] if len(p) > 1 else "") if n["name"] == "보관함" else n["name"]
                boxes.append({"box_id": n["id"], "box_name": n["name"],
                              "dept": dept, "path": " > ".join(p)})
        return boxes

    def _find_box(self, dept: str | None, box_id: str | None) -> dict:
        boxes = self.list_archive_boxes()
        if box_id:
            for b in boxes:
                if b["box_id"] == box_id:
                    return b
            return {"box_id": box_id, "box_name": "", "dept": "", "path": ""}
        if not dept:
            raise EdocError("dept(부서명) 또는 box_id 가 필요합니다. edoc_list_archive_boxes 로 목록을 확인하세요.")
        key = dept.replace(" ", "")
        hits = [b for b in boxes if key in b["dept"].replace(" ", "") or key in b["path"].replace(" ", "")]
        if not hits:
            raise EdocError(f"'{dept}' 보관함을 찾지 못했습니다. 접근 가능한 보관함: "
                           + ", ".join(b["dept"] for b in boxes))
        exact = [b for b in hits if b["dept"].replace(" ", "") == key]
        if len(exact) == 1:
            return exact[0]
        if len(hits) > 1:
            raise EdocError("여러 보관함이 해당됩니다. 더 구체적으로 지정하세요: " + ", ".join(b["path"] for b in hits))
        return hits[0]

    def list_archive_documents(self, dept=None, box_id=None, start_date=None, end_date=None,
                               keyword=None, page=1, page_size=50) -> dict:
        if not dept and not box_id or (dept or "").strip() in ("전체", "완료함", "all"):
            b = {"box_id": "", "path": "완료함(접근 가능한 보관함 전체)"}
        else:
            b = self._find_box(dept, box_id)
        data = {"facade": "AppDocFacade", "command": "listEndDoc", "nextpage": "/app/doc/endDocList.jsp",
                "menuKind": ARCHIVE, "isViewMode": "true",
                "paging_listcnt": str(page_size), "currpage_no": str(page),
                "srchWholeYn": "N", "srchInOutType": "ALL", "srchDocType": "ALL", "srchFormId": "ALL"}
        if b["box_id"]:
            data["srchBoxId"] = b["box_id"]
        if start_date:
            data["srchStartDate"] = start_date
        if end_date:
            data["srchEndDate"] = end_date
        if keyword:
            data["srchKeyword"] = keyword
        html = self._call("POST", CTRL, data=data)
        out = self._parse_list(html, ARCHIVE)
        out.update({"box": ARCHIVE, "box_name": b["path"] or "보관함", "box_id": b["box_id"],
                    "page": page, "page_size": page_size})
        return out

    # ------------------------------------------------------------ 본문
    def get_document(self, instance_id: str, ref_id: str, box="ENDLIST",
                     include_html=False, include_line=True) -> dict:
        box = resolve_box(box)
        if self._is_converted(instance_id, ref_id):
            return self._get_converted_document(instance_id, ref_id, box, include_html, include_line)
        html = self._call("POST", CTRL, data={
            "nextpage": "/app/wih/wihMain.jsp", "instanceId": instance_id,
            "refId": ref_id, "menuKind": box, "isViewMode": "true"})
        if "결재문서조회 오류" in html or "요청하신 페이지를 찾을 수 없습니다" in html:
            raise EdocError("문서를 열 수 없습니다. (삭제됐거나 권한이 없는 문서일 수 있습니다)")
        soup = BeautifulSoup(html, "html.parser")

        def hv(name):
            el = soup.find("input", attrs={"name": name})
            return el.get("value", "") if el is not None else ""

        area = soup.find(id="formarea")
        body_text = html_to_text(area) if area else ""
        doc = {
            "ref_id": ref_id, "instance_id": instance_id, "box": box,
            "doc_no": hv("docNo"), "title": hv("title"), "form_alias": hv("formAlias"),
            "status": hv("status"), "body_text": body_text,
            "attachments": self._parse_attachments(html, soup),
        }
        if include_html and area is not None:
            for s in area.find_all("script"):
                s.decompose()
            doc["body_html"] = str(area)
        if include_line:
            try:
                doc.update(self.get_approval_line(ref_id, box))
            except EdocError as e:
                doc["approval_line_error"] = str(e)
        return doc

    # ------------------------------------------------------------ 옛 문서(이관 문서)
    @staticmethod
    def _is_converted(instance_id: str, ref_id: str) -> bool:
        """예전 그룹웨어에서 옮겨온 문서(대략 2019년 이전)는 ref_id 가 MIG 로 시작한다."""
        return (ref_id or "").startswith("MIG") or str(instance_id) == "1"

    def _get_converted_document(self, instance_id, ref_id, box, include_html, include_line) -> dict:
        menu = {"APP_SHARE": "SHARELIST"}.get(box, box)
        html = self._call("POST", BASE + "/app/conversion/index.jsp", data={
            "isViewMode": "true", "appId": ref_id, "menuKind": menu,
            "addParamJson": "{}", "isPortal": "false"})
        soup = BeautifulSoup(html, "html.parser")
        area = soup.find(id="mainForm") or soup.find(id="formarea") or soup.body
        if area is None:
            raise EdocError("옛 문서를 열 수 없습니다. (이관 문서 화면 구조가 예상과 다릅니다)")
        for t in area.find_all(["script", "style"]):
            t.decompose()
        body_text = html_to_text(area)
        body_text = "\n".join(ln for ln in body_text.split("\n")
                               if not re.match(r"\s*var url\s*=|.*InnoFD", ln))
        if len(body_text) < 20:
            raise EdocError("옛 문서 본문을 찾지 못했습니다.")

        def pick(label):
            m = re.search(label + r"\s*\|\s*([^\n|]+)", body_text)
            if not m:
                return ""
            v = re.split(r"\s+(관련문서|코칭사항|보관함|수신처)\b", m.group(1))[0]
            return v.strip()[:150]

        doc = {
            "ref_id": ref_id, "instance_id": instance_id, "box": box, "converted": True,
            "doc_no": pick("문서번호"), "title": pick(r"제\s*목"), "form_alias": "",
            "status": "", "body_text": body_text,
            "attachments": self._parse_attachments(html, soup) + self._parse_conv_attachments(html),
            "note": "예전 그룹웨어에서 옮겨온 문서입니다.",
        }
        if include_html:
            doc["body_html"] = str(area)
        if include_line:
            try:
                doc.update(self.get_approval_line(ref_id, box))
            except EdocError as e:
                doc["approval_line_error"] = str(e)
        return doc

    @staticmethod
    def _parse_conv_attachments(html: str) -> list:
        """옛 문서의 첨부(ConvDownController). file_id 는 'conv:' + 내려받기 주소의 쿼리."""
        qs = re.findall(r'file_list\.push\("([^"]+)"\)', html)
        names = re.findall(r'file_name\.push\("([^"]*)"\)', html)
        sizes = re.findall(r'file_size\.push\((\d+)\)', html)
        out, seen = [], set()
        for i, q in enumerate(qs):
            if q in seen:
                continue
            seen.add(q)
            out.append({"file_id": "conv:" + q,
                        "name": names[i] if i < len(names) else "",
                        "size": int(sizes[i]) if i < len(sizes) else None})
        return out

    @staticmethod
    def _parse_attachments(html: str, soup) -> list:
        files, seen = [], set()
        pat = re.compile(r'_fileArr\.push\(\s*\[\s*url\s*\+\s*["\']\?fileId=([^"\'&]+)[^"\']*["\']\s*,\s*'
                         r'["\']((?:[^"\'\\]|\\.)*)["\']\s*,\s*["\']?(\d*)')
        for fid, name, size in pat.findall(html):
            if fid in seen:
                continue
            seen.add(fid)
            files.append({"file_id": fid, "name": name.replace("\\'", "'").replace('\\"', '"'),
                          "size": int(size) if size else None})
        if not files:  # 예비: 이미 그려진 목록이 있을 때
            for li in soup.select("li.filelistLI"):
                fid = li.get("id")
                nm = li.select_one(".file_name")
                if fid and fid not in seen:
                    seen.add(fid)
                    files.append({"file_id": fid, "name": _clean(nm.get_text(" ")) if nm else "", "size": None})
        return files

    # ------------------------------------------------------------ 결재선·의견
    def get_approval_line(self, ref_id: str, box="ENDLIST") -> dict:
        html = self._call("GET", CTRL, params={
            "facade": "AppLineFacade", "command": "appLineInfo",
            "nextpage": "/app/pop/appLineInfoPop.jsp", "ximYn": "N", "appId": ref_id,
            "userType": "", "cefIsEndList": "true" if resolve_box(box) in ("ENDLIST", ARCHIVE) else "false"})
        soup = BeautifulSoup(html, "html.parser")
        groups = []
        for gi, sec in enumerate(soup.select("div.leave_section")):
            steps = []
            for order, li in enumerate(sec.select("ul.leave_section_box > li"), start=1):
                for conts in li.select("div.leave_conts"):
                    head = conts.select_one(".conts_head")
                    info = conts.select_one(".user_info")
                    name = info.select_one("a") if info else None
                    dept = info.find("div") if info else None
                    state = conts.select_one(".now_state")
                    times = [_clean(d.get_text(" ")) for d in state.find_all("div", recursive=False)] if state else []
                    steps.append({
                        "order": order,
                        "role": _clean(head.get_text(" ")) if head else "",
                        "dept": _clean(dept.get_text(" ")) if dept else "",
                        "name": _clean(name.get_text(" ")) if name else (_clean(info.get_text(" ")) if info else ""),
                        "signed": times[0][1:].strip() if times and times[0].startswith("S") else (times[0] if times else ""),
                        "read": times[1][1:].strip() if len(times) > 1 and times[1].startswith("R") else (times[1] if len(times) > 1 else ""),
                    })
            groups.append({"group": gi + 1, "steps": steps})
        opinions = []
        for li in soup.select("div.approval_opinion li"):
            u = li.select_one(".user_info")
            c = li.select_one(".user_comment")
            t = li.select_one(".approval_opinion_time")
            if c is None:
                continue
            opinions.append({"user": _clean(u.get_text(" ")) if u else "",
                             "comment": _clean(c.get_text("\n")),
                             "time": _clean(t.get_text(" ")) if t else ""})
        return {"approval_line": groups, "opinions": opinions}

    # ------------------------------------------------------------ 첨부
    def download_attachment(self, file_id: str, save_dir: str, file_name: str | None = None) -> str:
        if not self.logged_in:
            self.login()
        save = Path(save_dir)
        save.mkdir(parents=True, exist_ok=True)
        for attempt in range(2):
            self._throttle()
            if file_id.startswith("conv:"):
                q = file_id[5:]
                r = self.s.get(BASE + "/ConvDownController" + (q if q.startswith("?") else "?" + q),
                               timeout=120, stream=True)
            else:
                r = self.s.get(BASE + "/DownController.do", params={"fileId": file_id}, timeout=120, stream=True)
            ctype = r.headers.get("Content-Type", "")
            if "text/html" in ctype:
                html = self._text(r)
                if self._is_logged_out(html) and attempt == 0:
                    self.logged_in = False
                    self.login()
                    continue
                raise EdocError("첨부파일을 받지 못했습니다. (파일이 없거나 권한이 없을 수 있습니다)")
            break
        conv_name = None
        if file_id.startswith("conv:"):
            m = re.search(r"down_filename=([^&]+)", file_id)
            conv_name = unquote(m.group(1)) if m else None
        name = (file_name or self._filename_from(r.headers.get("Content-Disposition", ""))
                or conv_name or file_id)
        name = re.sub(r'[\\/:*?"<>|]', "_", name)
        path = save / name
        if path.exists():
            stem, suf = path.stem, path.suffix
            i = 2
            while (save / f"{stem} ({i}){suf}").exists():
                i += 1
            path = save / f"{stem} ({i}){suf}"
        with open(path, "wb") as f:
            for chunk in r.iter_content(65536):
                f.write(chunk)
        return str(path)

    @staticmethod
    def _filename_from(cd: str) -> str | None:
        m = re.search(r"filename\*=(?:UTF-8'')?([^;]+)", cd, re.I)
        if m:
            return unquote(m.group(1).strip().strip('"'))
        m = re.search(r'filename="?([^";]+)"?', cd, re.I)
        if m:
            raw = m.group(1)
            try:
                return unquote(raw.encode("latin-1").decode("utf-8"))
            except UnicodeError:
                return unquote(raw)
        return None
