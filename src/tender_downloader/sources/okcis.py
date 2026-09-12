"""OKCIS's normal browser search, including human verification and pagination."""
from __future__ import annotations

import json
import re
from datetime import date
from urllib.parse import urlsplit

from ..browser_cdp import BrowserCdpTransport, BrowserTransportError
from ..http_client import FetchError, SourceBlocked
from ..models import AttachmentRef, Coverage, Notice, RawDocument
from .custom_web import CustomWebSource

ORIGIN = "https://www.okcis.cn"
SEARCH = ORIGIN + "/search/"

# Read only the result iframe or notice content; never collect account fields,
# cookies, hidden form tokens, or the office's unrelated subscription results.
SNAPSHOT = r"""(() => {
  let d = document;
  const iframe = document.querySelector('#sosoIframe');
  try { if (iframe?.contentDocument?.body) d = iframe.contentDocument; } catch {}
  const text = d.body?.innerText || '';
  const challenge = /slide jigsaw to complete verification|验证码|人机验证|访问验证|verify you are human|checking your browser/i.test(text);
  if (challenge) return {challenge:true, framed:d!==document};
  const login = [...d.querySelectorAll('input[type=password]')].some(e => e.getClientRects().length);
  if (login) return {login:true};
  const rows = [...d.querySelectorAll('a[name="result-list-title"]')].map(a => {
    const row = a.closest('ul'); const info = row?.querySelector('[rec_jointime]');
    return {url:a.href, title:(a.getAttribute('title')||a.innerText).trim(),
      date:info?.getAttribute('rec_jointime')||'', type:info?.getAttribute('rec_infoname')||'',
      region:(row?.querySelector('li:last-child b:last-child')?.innerText||'').replace(/\s*\d{4}-\d{2}-\d{2}.*/, '').trim()};
  });
  if (!rows.length) {
    // New-style result markup: the title anchor carries no name attribute; the
    // row's bookmark <b> holds rec_jointime/rec_infoname/rec_title metadata.
    const seen = new Set();
    rows.push(...[...d.querySelectorAll('a[href]')].filter(a => {
      const href=a.getAttribute('href')||'';
      return /^\/\d{8}-[np]\d+-\d+\.html$/.test(href) && !seen.has(href) && seen.add(href);
    }).map(a => {
      const row = a.closest('ul'); const info = row?.querySelector('b[rec_jointime]');
      return {url:a.href, title:(a.getAttribute('title')||info?.getAttribute('rec_title')||a.innerText).trim(),
        date:info?.getAttribute('rec_jointime')||'', type:info?.getAttribute('rec_infoname')||'',
        region:(row?.querySelector('li:last-child b:last-child')?.innerText||'').replace(/\s*\d{4}-\d{2}-\d{2}.*/, '').trim()};
    }));
  }
  const next = [...d.querySelectorAll('a')].find(a => a.innerText.trim()==='下一页' && /^loadnextpage\(['"]\d+['"]\)/.test(a.getAttribute('onclick')||''));
  const keyword = d.querySelector('form[name="result-form-search"] [name="result-keyword-and-form"]')?.value;
  const contentMode = d.querySelector('form[name="result-form-search"] [name="result-content-type-form"]')?.value;
  const dates = ['search-start-time-input','search-end-time-input'].map(n => d.querySelector(`form[name="result-form-search"] [name="${n}"]`)?.value || '');
  const main = document.querySelector('#copyquyu');
  let html = ''; const attachments = [];
  if (main) {
    const clone=main.cloneNode(true);
    clone.querySelectorAll('script,style,textarea,input,button,[style*="display: none"],.copybefore').forEach(e=>e.remove());
    clone.querySelectorAll('[onclick]').forEach(e=>e.removeAttribute('onclick'));
    html=clone.outerHTML;
    main.querySelectorAll('[onclick]').forEach(a=>{
      const m=(a.getAttribute('onclick')||'').match(/^savefujian\('([^']+)','([01])','(\d+)'\)$/);
      if(!m) return;
      // The #downfileurl input is only rendered for premium tiers; an empty
      // value still downloads for this membership, so never emit "undefined".
      const down=document.getElementById('downfileurl'+m[3])?.value||'';
      attachments.push({url:m[2]==='1' ? new URL('/php/savefuj.php?fj='+m[1]+'&downfileurl='+encodeURIComponent(down), location.origin).href : '',
        label:a.closest('li')?.querySelector('.fjyl_num')?.innerText?.trim() || m[1].split('/').pop(), restricted:m[2]!=='1'});
    });
  }
  return {url:location.href, challenge:false, rows, next:!!next, keyword, contentMode, dates,
    empty:/没有找到|未找到.*信息|暂无.*信息|没有符合|共\s*0\s*条/.test(text),
    result:!!d.querySelector('form[name="result-form-search"]'), returnedToSearch:d!==document && !!d.querySelector('#k1'),
    html, attachments};
})()"""


def search_action(term: str, start: date, end: date, content_type: str = "2",
                  region_code: str = "220000") -> str:
    values = json.dumps([term, start.isoformat(), end.isoformat(), content_type, region_code], ensure_ascii=False)
    return r"""(() => {
      const [term,start,end,contentType,regionCode] = VALUES;
      const form=document.querySelector('#search-form-on');
      if(!form || new URL(form.action).origin!==location.origin) return false;
      // The site periodically rebuilds the result iframe under a new random
      // name; a long-lived tab can keep a form target that no longer exists,
      // making every submit silently go nowhere. Repoint it before searching.
      if (form.target && !document.querySelector('iframe[name="'+form.target+'"]')) {
        const live = document.querySelector('#sosoIframe');
        if (live && live.name) form.target = live.name;
      }
      // The site's submit handler routes to a classic-mode result iframe when
      // this tab is styled as 经典搜索; that variant lacks the row structure
      // the collector parses. Normalize to the new-style result page.
      const mode=document.getElementById('search-type-jingdian');
      if(mode && mode.getAttribute('class')==='dcss_g'){ mode.setAttribute('class',''); }
      const set=(selector,value)=>{const e=form.querySelector(selector);if(!e) throw Error('搜索控件缺失');e.value=value;e.dispatchEvent(new Event('change',{bubbles:true}));};
      set('#k1',term);set('#k2','');
      form.querySelectorAll('input[type=checkbox]').forEach(e=>{e.checked=false;});
      const region=regionCode
        ? form.querySelector('[name="city-result-city-input[]"][value="'+regionCode+'"]')
        : form.querySelector('#city-result-city-input-quanguo');
      const allTypes=form.querySelector('[name="result-infotype-input[]"][value="un"]');
      if(!region || !allTypes) return false;
      region.checked=true;allTypes.checked=true;
      form.querySelector('[name="search-time-type"][value="zidingyi"]').checked=true;
      set('#search-start-time-input',start);set('#search-end-time-input',end);
      // contentType=2 (title match) can start answering with an empty list
      // while the session is throttled; 1 (full text) keeps working, so the
      // caller falls back to it and this form must honour the request.
      const content=form.querySelector('[name="result-content-type-form"][value="'+contentType+'"]');
      if(content) content.checked=true;
      form.querySelector('[name="result-auto-type-form"][value="2"]').checked=true;
      const button=[...document.querySelectorAll('a[name="search-button-search-on-new"]')].find(a=>a.innerText.trim()==='立即搜索');
      if(!button) return false;
      button.click();return true;
    })()""".replace("VALUES", values)


NEXT_ACTION = r"""(() => {
  const d=document.querySelector('#sosoIframe')?.contentDocument;
  const next=[...(d?.querySelectorAll('a')||[])].find(a=>a.innerText.trim()==='下一页' && /^loadnextpage\(['"]\d+['"]\)/.test(a.getAttribute('onclick')||''));
  if(!next) return false; next.click(); return true;
})()"""


# Read one notice page through the logged-in session's fetch channel and parse
# the same body/attachment structure the live detail page exposes. __URL__ is a
# JSON-encoded absolute URL injected by the Python caller.
DETAIL_FETCH = r"""(async () => {
  const r = await fetch(__URL__, {cache: 'no-store'});
  // Notice pages are GBK; Response.text() would force UTF-8 mojibake.
  const text = new TextDecoder('gbk').decode(await r.arrayBuffer());
  if (/captchaPage|id="infoString"|doVerify\.php/.test(text)) return {challenged: true};
  // A single notice can require a higher membership tier and answer with a
  // login form even though the session itself is fine; that is a per-notice
  // restriction, not a session-wide block.
  if (/<input[^>]+type=["']password["']/.test(text)) return {challenged: false, html: '', login_required: true};
  const doc = new DOMParser().parseFromString(text, 'text/html');
  // .xiangmu_bodys is the notice body in the static HTML; #copyquyu only
  // exists on the rendered page for categories carrying attachments.
  const main = doc.querySelector('.xiangmu_bodys') || doc.querySelector('#copyquyu');
  if (!main) return {challenged: false, html: ''};
  // A real login/permission page is a short shell; masked-contact links were
  // already stripped above, so any remaining login marker means the whole
  // notice is gated.
  const bodyHtml = main.outerHTML;
  if (bodyHtml.length < 800
    && (/<input[^>]+type\s*=\s*['"]?password/i.test(bodyHtml)
      || /登录后(?:查看|下载|访问|获取)|请先登录|账号密码登录/.test(bodyHtml))) {
    return {challenged: false, html: '', login_required: true};
  }
  const clone = main.cloneNode(true);
  clone.querySelectorAll('script,style,textarea,input,button,[style*="display: none"],.copybefore').forEach(e=>e.remove());
  clone.querySelectorAll('[onclick]').forEach(e=>e.removeAttribute('onclick'));
  // Contact fields are masked as "登录后查看" login links; the notice body
  // itself is complete. Replace those links with plain text so neither the
  // solver nor the downstream content gate mistakes this for a login page.
  clone.querySelectorAll('a[href*="/login/"]').forEach(e=>e.replaceWith('（登录后可见）'));
  const attachments = [];
  main.querySelectorAll('[onclick]').forEach(a=>{
    const m=(a.getAttribute('onclick')||'').match(/^savefujian\('([^']+)','([01])','(\d+)'\)$/);
    if(!m) return;
    // The #downfileurl input is only rendered for premium tiers; an empty
    // value still downloads for this membership, so never emit "undefined".
    const down=doc.getElementById('downfileurl'+m[3])?.value||'';
    attachments.push({url:m[2]==='1' ? new URL('/php/savefuj.php?fj='+m[1]+'&downfileurl='+encodeURIComponent(down), location.origin).href : '',
      label:a.closest('li')?.querySelector('.fjyl_num')?.innerText?.trim() || m[1].split('/').pop(), restricted:m[2]!=='1'});
  });
  // The static HTML carries the attachment path in rec_fuj attributes even
  // when the rendered download button (and its savefujian onclick) is absent.
  doc.querySelectorAll('[rec_fuj]').forEach(el=>{
    const fj=el.getAttribute('rec_fuj')||'';
    if(!fj || attachments.some(a => a.url.indexOf(fj) > -1)) return;
    attachments.push({url:new URL('/php/savefuj.php?fj='+fj+'&downfileurl=', location.origin).href,
      label:fj.split('/').pop(), restricted:false});
  });
  return {challenged: false, html: clone.outerHTML, attachments};
})()"""


# Answer the site WAF's slider challenge for the logged-in member session so
# unattended collection can continue. The challenge page (or the fragment the
# WAF injects into the result area) carries an #infoString token; the puzzle
# gap is located from the served PNG's alpha channel because the hole is the
# only semi-transparent region, and the drag distance follows from the cutout
# shape's offset inside its own container. Trajectory telemetry is required by
# the endpoint but not validated server-side; a smooth synthetic path passes.
CAPTCHA_SOLVE = r"""(async () => {
  const findToken = () => {
    const docs = [document];
    const iframe = document.querySelector('#sosoIframe');
    try { if (iframe?.contentDocument) docs.push(iframe.contentDocument); } catch {}
    for (const d of docs) {
      const el = d.getElementById && d.getElementById('infoString');
      if (el) return el.value;
    }
    return null;
  };
  let binfo = findToken();
  if (!binfo) {
    // The challenge may sit in another tab or be injected into a result pane;
    // probing a normal page returns the challenge HTML with its token, which
    // is all the solver needs regardless of where the UI shows it.
    const probe = await fetch('/search/', {cache: 'no-store'});
    const text = await probe.text();
    const m = text.match(/id="infoString"\s+value="([^"]+)"/);
    if (!m) return {solved:false, reason:'no_challenge_page'};
    binfo = m[1];
  }
  const r1 = await fetch('/pepp5_captcha_get?width=300&height=180&domain=' + encodeURIComponent(location.hostname), {cache:'no-store'});
  if (!r1.ok) return {solved:false, reason:'get_failed'};
  const cap = await r1.json();
  const loadImage = (b64) => new Promise(res => {
    const img = new Image();
    img.onload = () => res(img);
    img.onerror = () => res(null);
    img.src = 'data:image/png;base64,' + b64;
  });
  const shade = await loadImage(cap.shadeImage);
  const cut = await loadImage(cap.cutoutImage);
  if (!shade || !cut) return {solved:false, reason:'image_failed'};
  const W = shade.width, H = shade.height;
  const cv = document.createElement('canvas'); cv.width = W; cv.height = H;
  const ctx = cv.getContext('2d', {willReadFrequently:true});
  ctx.drawImage(shade, 0, 0);
  const d = ctx.getImageData(0, 0, W, H).data;
  const colLow = [];
  for (let x = 0; x < W; x++) {
    let c = 0;
    for (let y = 0; y < H; y++) if (d[(y * W + x) * 4 + 3] < 250) c++;
    colLow.push(c);
  }
  const xs = [];
  for (let x = 0; x < W; x++) if (colLow[x] > 10) xs.push(x);
  if (!xs.length) return {solved:false, reason:'no_hole'};
  const holeX0 = Math.min(...xs);
  const cv2 = document.createElement('canvas'); cv2.width = cut.width; cv2.height = cut.height;
  const ctx2 = cv2.getContext('2d', {willReadFrequently:true});
  ctx2.drawImage(cut, 0, 0);
  const dd = ctx2.getImageData(0, 0, cut.width, cut.height).data;
  let shapeX0 = cut.width;
  for (let yy = 0; yy < cut.height; yy++)
    for (let xx = 0; xx < cut.width; xx++)
      if (dd[(yy * cut.width + xx) * 4 + 3] > 200 && xx < shapeX0) shapeX0 = xx;
  const guess = holeX0 - shapeX0;
  const r2 = await fetch('/pepp5_captcha_check', {
    method:'POST', cache:'no-store',
    headers:{'Content-Type':'application/json;charset=utf-8'},
    body: JSON.stringify({id: parseFloat(cap.id), x: guess, y: parseFloat(cap.y), b_info: binfo})
  });
  if ((await r2.text()) !== 'true') return {solved:false, reason:'check_failed', guess};
  const p2 = n => String(n).padStart(2, '0');
  const now = new Date();
  const stamp = now.getFullYear()+'-'+p2(now.getMonth()+1)+'-'+p2(now.getDate())+' '+p2(now.getHours())+':'+p2(now.getMinutes())+':'+p2(now.getSeconds());
  const pts = [];
  for (let i = 0; i <= 120; i++) {
    const t = i / 120;
    pts.push(Math.round(guess * (1 - Math.pow(1 - t, 2))) + ',' + (300 + Math.round(10 * Math.sin(i * 0.9))));
  }
  const r3 = await fetch('/pepp5_captcha_data', {
    method:'POST', cache:'no-store',
    headers:{'Content-Type':'application/json;charset=utf-8'},
    body: JSON.stringify({
      ci: (document.cookie.match(/HMF_CI=([^;]+)/) || [])[1] || '',
      starttime: stamp, endtime: stamp,
      axios: pts.join(';'), isAudio: '4', pointAxios: '', picAxios: '',
      device: 'web', screenSize: screen.width + ',' + screen.height
    })
  });
  return {solved: r3.ok, reason: r3.ok ? '' : 'report_failed', guess};
})()"""


class OKCISSource(CustomWebSource):
    display_name = "招标采购导航网（OKCIS）"

    def __init__(self, client, config):
        super().__init__(client, config)
        if self.start_urls != [SEARCH] or self.auth_mode != "browser":
            raise ValueError("导航网需配置 https://www.okcis.cn/search/ 和浏览器登录")
        self._progress = lambda state, message: None
        self._transport = None
        self._targets = {}
        self._attachments = {}
        self._detail_target = ""

    def set_progress_callback(self, callback):
        self._progress = callback

    def prepare(self):
        if self._prepared:
            return
        session = self._load_browser_session()
        self._transport = BrowserCdpTransport.from_descriptor(
            session.get("browser_transport"), expected_origin=ORIGIN,
            timeout_seconds=self.client.timeout_seconds)
        self.client.attach_browser_transport(self._transport)
        self._wait_page(SEARCH)
        self._prepared = True

    def _evaluate(self, url, expression, *, focus=False, await_promise=False):
        transport = self._transport
        if not transport or not transport.handles(url):
            raise SourceBlocked("导航网浏览器会话与来源不匹配")
        socket = transport._socket_factory(transport.websocket_url, transport.timeout_seconds)
        session_id = ""
        try:
            target = self._targets.get(url)
            if target:
                session_id = transport._command(socket, "Target.attachToTarget", {"targetId":target,"flatten":True})["sessionId"]
            else:
                if url == SEARCH:
                    target, session_id, exact = transport._select_page(socket, exact_url=url)
                    if not exact:
                        transport._command(socket, "Page.navigate", {"url":url}, session_id=session_id)
                else:
                    target, session_id, exact = transport._select_page(socket, exact_url=url)
                    if not exact:
                        transport._detach_quietly(socket, session_id)
                        target = self._detail_target
                        fresh = not target
                        if fresh:
                            target = transport._command(socket, "Target.createTarget", {"url":url})["targetId"]
                            self._detail_target = target
                        session_id = transport._command(socket, "Target.attachToTarget", {"targetId":target,"flatten":True})["sessionId"]
                        if not fresh:
                            transport._command(socket, "Page.navigate", {"url":url}, session_id=session_id)
                        self._targets = {key:value for key,value in self._targets.items() if value != target}
                self._targets[url] = target
            tree = transport._command(socket, "Page.getResourceTree", session_id=session_id)
            current = tree.get("frameTree", {}).get("frame", {}).get("url", "")
            if not current or current == "about:blank":
                return {}
            if not transport.handles(current):
                raise SourceBlocked("导航网页面离开授权来源（" + (urlsplit(current).hostname or urlsplit(current).scheme) + "），请人工检查")
            if focus:
                transport._command(socket, "Page.bringToFront", session_id=session_id)
            parameters = {"expression":expression,"returnByValue":True}
            if await_promise:
                # The challenge solver is an async IIFE; without awaiting the
                # promise the call returns a bare reference with no value.
                parameters["awaitPromise"] = True
            result = transport._command(socket, "Runtime.evaluate", parameters, session_id=session_id)
            if result.get("exceptionDetails"):
                raise FetchError("导航网页面结构已变化，搜索操作未完成")
            return result.get("result", {}).get("value", {})
        except BrowserTransportError as exc:
            raise SourceBlocked("导航网浏览器已关闭或连接失败，请重新登录后查询") from exc
        finally:
            if session_id:
                transport._detach_quietly(socket, session_id)
            socket.close()

    AUTO_SOLVE_ATTEMPTS = 3

    def _auto_solve_challenge(self, url):
        """Answer the member session's slider challenge; True when accepted."""
        outcome = self._evaluate(url, CAPTCHA_SOLVE, await_promise=True) or {}
        if outcome.get("solved"):
            self._progress("running", "导航网滑块验证已自动完成，继续采集。")
            return True
        return False

    def _wait_page(self, url, *, results=False, previous=(), retry_action=None):
        try:
            return self._wait_page_once(url, results=results, previous=previous, retry_action=retry_action)
        except FetchError:
            if not (results and retry_action):
                raise
            # A submitted search can die silently (e.g. a stale form target
            # rebuilt by the site) leaving no challenge to detect; replay the
            # same search once before declaring failure.
            self._transport._sleeper(2)
            if self._evaluate(url, retry_action) is not True:
                raise
            return self._wait_page_once(url, results=results, previous=previous, retry_action=retry_action)

    def _wait_page_once(self, url, *, results=False, previous=(), retry_action=None):
        transport = self._transport
        deadline = transport._clock() + 40
        waiting = False
        resubmitted = False
        auto_attempts = 0
        while transport._clock() < deadline:
            state = self._evaluate(url, SNAPSHOT)
            if state.get("challenge") or state.get("login"):
                if state.get("challenge") and auto_attempts < self.AUTO_SOLVE_ATTEMPTS:
                    # The challenge targets this tool's own logged-in session;
                    # answer it automatically before asking a human.
                    auto_attempts += 1
                    if self._auto_solve_challenge(url):
                        if retry_action:
                            self._evaluate(url, retry_action)
                        elif url != SEARCH:
                            self._evaluate(url, "(() => {location.reload(); return true;})()")
                        transport._sleeper(2)
                        continue
                if not waiting:
                    deadline = transport._clock() + int(self.config.get("verification_wait_seconds", 900))
                    waiting = True
                    self._progress("waiting_verification", "导航网需要人工验证：自动应答未成功，请在已打开的浏览器完成验证码或登录；完成后自动继续，已采集结果可随时导出。")
                    # Remove only the site's loading spinner, which otherwise
                    # covers the embedded challenge. The CAPTCHA is untouched.
                    self._evaluate(url, "(() => {window.layer?.closeAll('loading');document.querySelector('#sosoIframe')?.scrollIntoView();return true;})()", focus=True)
                transport._sleeper(2)
                continue
            if state.get("returnedToSearch") and retry_action and not resubmitted:
                # This site's WAF can bounce a POST (with or without a served
                # challenge) back to the bare search form. Repeat the same
                # normal search once; the page deadline bounds retries.
                self._evaluate(url, retry_action)
                resubmitted = True
                transport._sleeper(2)
                continue
            rows = state.get("rows", [])
            fingerprint = tuple(row["url"] for row in rows)
            ready = (state.get("result") and (rows or state.get("empty")) and (not previous or fingerprint != previous)) if results else (state.get("url") == url and (state.get("html") or url == SEARCH))
            if ready:
                if waiting:
                    self._progress("running", "人工验证已通过，继续采集。")
                return state
            transport._sleeper(1)
        if waiting:
            raise SourceBlocked("验证码/登录等待超时；已保留采集结果，请完成验证后重新查询")
        raise FetchError("导航网没有返回有效结果或翻页无变化，不能判定为零条公告")

    def iter_notices(self, start: date, end: date, coverage: Coverage):
        self.prepare()
        terms = [term for term in self.config.get("_query_terms", [""]) if term]
        if not terms:
            coverage.truncated = True
            coverage.status = "partial"
            coverage.message = "导航网不接受空关键词；本来源未执行无关键词广搜，不能保证防漏。请填写关键词或选择行业。"
            return
        seen = set()
        max_pages = int(self.config.get("max_pages", 100))
        full_text = self.config.get("_query_criteria", {}).get("mode") == "ai_recall"
        for term in terms:
            from ..geography import source_region
            selected_region = source_region(self.config)
            action = search_action(term, start, end, content_type="1" if full_text else "2",
                                   region_code=selected_region["province_code"])
            if self._evaluate(SEARCH, action) is not True:
                raise FetchError("导航网搜索表单不可用，请重新打开高级搜索页")
            previous = ()
            first_page = None
            verification_resets = 0
            expected_mode = "1" if full_text else "2"
            for page in range(max_pages):
                try:
                    state = self._wait_page(SEARCH, results=True, previous=previous, retry_action=action)
                except FetchError:
                    if page or expected_mode == "1":
                        raise
                    # Title mode can answer with a permanently empty list while
                    # the session is throttled; retry the same term once in
                    # full-text mode instead of reporting zero coverage.
                    expected_mode = "1"
                    action = search_action(term, start, end, content_type="1",
                                           region_code=selected_region["province_code"])
                    if self._evaluate(SEARCH, action) is not True:
                        raise
                    state = self._wait_page(SEARCH, results=True, previous=previous, retry_action=action)
                if state.get("keyword") != term or state.get("contentMode") != expected_mode or state.get("dates") != [start.isoformat(), end.isoformat()]:
                    raise FetchError("导航网未确认当前关键词/日期条件，已停止，避免混入旧结果")
                coverage.pages += 1
                rows = state["rows"]
                previous = tuple(row["url"] for row in rows)
                # After an auto-solved challenge the recovery action re-runs the
                # search from page one; already collected rows are deduplicated
                # by `seen`, but endless challenge/re-solve loops must stop.
                if first_page is None:
                    first_page = previous
                elif page > 0 and previous == first_page:
                    verification_resets += 1
                    if verification_resets >= 2:
                        coverage.truncated = True
                        coverage.message = "导航网翻页验证自动恢复后再次回到第一页；剩余页面未采集，结果可能不全。"
                        break
                for row in rows:
                    url = row["url"]
                    if not re.fullmatch(r"/\d{8}-[np]\d-\d+\.html", urlsplit(url).path) or not self._transport.handles(url):
                        raise FetchError("导航网公告地址结构发生变化")
                    try:
                        published = date.fromisoformat(row["date"])
                    except ValueError as exc:
                        raise FetchError("导航网结果缺少发布日期，不能判断覆盖范围") from exc
                    if not start <= published <= end or url in seen:
                        continue
                    seen.add(url)
                    coverage.notices += 1
                    coverage.first_date = min(coverage.first_date or row["date"], row["date"])
                    coverage.last_date = max(coverage.last_date, row["date"])
                    yield Notice(self.name, self.authority_rank, urlsplit(url).path[1:-5], row["title"], row["date"], url,
                        region=row["region"], notice_type=row["type"], metadata={"search_mode":"导航网按所选省份及平台包含的跨省信息检索",
                            "city": ("吉林市" if row["region"] == "吉林-吉林" else row["region"].removeprefix("吉林-"))})
                if not state["next"]:
                    break
                if page + 1 == max_pages:
                    coverage.truncated = True
                    coverage.message = f"导航网达到 {max_pages} 页分页上限，仍有未采集页面；请在数据来源中提高查询页数或缩短日期范围。"
                    break
                self._transport._sleeper(3)
                if self._evaluate(SEARCH, NEXT_ACTION) is not True:
                    raise FetchError("导航网下一页不可用，结果不完整")
        coverage.reached_start = coverage.reached_end = not coverage.truncated
        if not coverage.message:
            coverage.message = ("导航网按所选地区、日期和全文关键词查询；附件中的独有内容仍可能遗漏。" if full_text else
                                "导航网按所选地区、日期和标题关键词查询；仅正文或附件匹配的公告可能遗漏。")

    def _fetch_detail_state(self, url, attempt=0):
        """Fetch a notice page over the browser session's fetch channel.

        Tab navigations to detail URLs get served the WAF's arithmetic-code
        page (which this tool cannot and will not auto-answer), while the
        same-origin fetch channel serves the slider challenge our solver
        handles. Reading via fetch keeps the normal logged-in session.
        """
        expression = DETAIL_FETCH.replace("__URL__", json.dumps(url))
        state = self._evaluate(SEARCH, expression, await_promise=True) or {}
        if state.get("challenged"):
            if attempt >= self.AUTO_SOLVE_ATTEMPTS:
                raise FetchError("导航网详情页访问持续被验证码拦截")
            if not self._auto_solve_challenge(SEARCH):
                raise FetchError("导航网详情页验证码自动应答未成功")
            self._transport._sleeper(2)
            return self._fetch_detail_state(url, attempt + 1)
        if state.get("login_required"):
            # Skip just this notice (tier-gated content) instead of failing the
            # whole source; the session itself remains usable for other pages.
            raise FetchError("该公告内容需要更高会员权限，已跳过")
        if not state.get("html"):
            raise FetchError("导航网详情页内容为空或结构已变化")
        return state

    def fetch_detail(self, notice):
        state = self._fetch_detail_state(notice.url)
        self._attachments[notice.url] = state.get("attachments", [])
        notice.metadata["restricted_attachment_count"] = sum(item["restricted"] for item in self._attachments[notice.url])
        body = state["html"].encode("utf-8")
        return RawDocument(body, notice.url, {"content-type":"text/html; charset=utf-8"}, body)

    def discover_artifacts(self, notice, detail):
        # A savefujian argument is a storage key, not a relative download URL.
        # Generic onclick extraction must not manufacture a dead /2026/... URL.
        clean = re.sub(r"""\s+onclick=(?:"[^"]*"|'[^']*')""", "", self._analysis_bytes(detail).decode("utf-8"), flags=re.I)
        safe_detail = RawDocument(clean.encode("utf-8"), detail.url, detail.headers)
        refs = list(super().discover_artifacts(notice, safe_detail))
        refs.extend(AttachmentRef(item["url"], item["label"], item["label"], "public_browser", notice.url)
                    for item in self._attachments.get(notice.url, []) if item["url"])
        return list({ref.url: ref for ref in refs}.values())
