from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit, urlunsplit


# This fixed, read-only expression deliberately returns only three booleans.
# No page text, URL, credential, cookie, or other browser capability crosses
# the CDP boundary while duplicate exact-URL tabs are ranked.
_EXACT_PAGE_SELECTION_PROBE = r"""(() => {
  const bodyText = document.body
    ? (document.body.innerText || document.body.textContent || "")
    : "";
  const text = ((document.title || "") + "\n" + bodyText)
    .slice(0, 50000)
    .toLowerCase();
  const challengeText = /验证码|访问验证|安全验证|人机验证|请完成(?:以下)?验证|拖动[^\n]{0,20}滑块|点击[^\n]{0,20}验证|captcha|verify you are human|checking your browser|security check|just a moment|slide jigsaw to complete verification/.test(text);
  const challengeElement = document.querySelector(
    'iframe[src*="captcha" i], iframe[src*="verify" i], '
    + '[id*="captcha" i], [class*="captcha" i], '
    + '[id="nc_1_n1z"], [class*="nc_wrapper"]'
  ) !== null;
  return {
    visible: document.visibilityState === "visible" && !document.hidden,
    focused: typeof document.hasFocus === "function" && document.hasFocus(),
    challenge: challengeText || challengeElement
  };
})()"""


class BrowserTransportError(RuntimeError):
    """A bounded failure while reusing the user-approved browser session."""


class _SocketLike(Protocol):
    def send_json(self, payload: dict[str, Any]) -> None: ...

    def receive_json(self) -> dict[str, Any]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class BrowserResource:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes = b""
    size: int = 0
    # Optional rendered DOM used only for analysis. ``body`` remains the
    # immutable source response and is the only value written to the vault.
    analysis_body: bytes | None = None


def _origin(url: str) -> tuple[str, str, int]:
    try:
        parsed = urlsplit(url)
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise BrowserTransportError("浏览器会话 URL 端口无效") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise BrowserTransportError("浏览器会话 URL 无效")
    return parsed.scheme.lower(), parsed.hostname.lower().rstrip("."), port


def _default_socket_factory(url: str, timeout: float) -> _SocketLike:
    # Reuse the small, dependency-free RFC6455 implementation used by the
    # visual-login owner.  Import lazily so the crawler CLI has no UI startup
    # side effects.
    from .webui.browser_login import _WebSocket

    return _WebSocket(url, timeout=timeout)


class BrowserCdpTransport:
    """Read exact-origin resources through an already authenticated Chromium tab.

    The WebSocket URL is a short-lived loopback capability supplied only in the
    crawler child's environment.  This class neither reads cookies nor attempts
    to solve or bypass a CAPTCHA.  If the browser shows a challenge, its bytes
    are returned normally and the existing blocked-page detector stops the run.
    """

    KIND = "cdp-load-network-resource-v1"

    def __init__(
        self,
        websocket_url: str,
        origin: str,
        *,
        timeout_seconds: float = 35,
        socket_factory: Callable[[str, float], _SocketLike] = _default_socket_factory,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        parsed_ws = urlsplit(websocket_url)
        try:
            ws_port = parsed_ws.port
        except ValueError as exc:
            raise BrowserTransportError("浏览器调试端口无效") from exc
        if (
            parsed_ws.scheme != "ws"
            or parsed_ws.hostname not in {"127.0.0.1", "localhost", "::1"}
            or ws_port is None
            or not 1 <= ws_port <= 65_535
            or not parsed_ws.path.startswith("/devtools/browser/")
            or parsed_ws.username
            or parsed_ws.password
        ):
            raise BrowserTransportError("浏览器调试地址必须是本机 browser CDP WebSocket")
        parsed_origin = urlsplit(origin)
        origin_tuple = _origin(origin)
        if parsed_origin.path not in {"", "/"} or parsed_origin.query or parsed_origin.fragment:
            raise BrowserTransportError("浏览器会话必须绑定单一来源")
        self.websocket_url = websocket_url
        self.origin = origin_tuple
        self.timeout_seconds = max(1.0, min(float(timeout_seconds), 900.0))
        self._socket_factory = socket_factory
        self._clock = clock
        self._sleeper = sleeper
        self._next_id = 1
        # A detail URL receives at most one visible page action (an exact
        # same-origin fallback navigation only; an already exact tab is never
        # reloaded) for the lifetime of this transport. prepare(), enumeration,
        # and detail fetch commonly request the same seed, so later phases only
        # reuse the page.
        self._exact_action_attempted: set[str] = set()
        self._exact_action_failed: set[str] = set()

    @classmethod
    def from_descriptor(
        cls,
        value: object,
        *,
        expected_origin: str,
        timeout_seconds: float = 35,
        socket_factory: Callable[[str, float], _SocketLike] = _default_socket_factory,
    ) -> "BrowserCdpTransport":
        if not isinstance(value, dict) or value.get("kind") != cls.KIND:
            raise BrowserTransportError("浏览器会话描述符无效")
        websocket_url = value.get("websocket_url")
        origin = value.get("origin")
        if not isinstance(websocket_url, str) or not isinstance(origin, str):
            raise BrowserTransportError("浏览器会话描述符不完整")
        transport = cls(
            websocket_url,
            origin,
            timeout_seconds=timeout_seconds,
            socket_factory=socket_factory,
        )
        if transport.origin != _origin(expected_origin):
            raise BrowserTransportError("浏览器会话与当前来源不匹配")
        return transport

    def handles(self, url: str) -> bool:
        try:
            return _origin(url) == self.origin
        except BrowserTransportError:
            return False

    def _command(
        self,
        websocket: _SocketLike,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        command_id = self._next_id
        self._next_id += 1
        payload: dict[str, Any] = {
            "id": command_id,
            "method": method,
            "params": params or {},
        }
        if session_id:
            payload["sessionId"] = session_id
        websocket.send_json(payload)
        while True:
            response = websocket.receive_json()
            if response.get("id") != command_id:
                continue
            error = response.get("error")
            if error:
                message = error.get("message", "") if isinstance(error, dict) else ""
                raise BrowserTransportError(
                    f"浏览器资源请求失败：{str(message)[:300] or 'CDP 错误'}"
                )
            result = response.get("result", {})
            if not isinstance(result, dict):
                raise BrowserTransportError("浏览器资源响应格式无效")
            return result

    @staticmethod
    def _without_fragment(url: str) -> str:
        parsed = urlsplit(url)
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, parsed.query, "")
        )

    def _select_page(
        self,
        websocket: _SocketLike,
        *,
        exact_url: str,
    ) -> tuple[str, str, bool]:
        result = self._command(websocket, "Target.getTargets")
        candidates = result.get("targetInfos", [])
        if not isinstance(candidates, list):
            candidates = []
        exact_target_ids: list[str] = []
        fallback_target_id = ""
        for candidate in candidates:
            if not isinstance(candidate, dict) or candidate.get("type") != "page":
                continue
            url = candidate.get("url", "")
            if isinstance(url, str) and self.handles(url):
                raw_id = candidate.get("targetId", "")
                if isinstance(raw_id, str) and raw_id:
                    if not fallback_target_id:
                        fallback_target_id = raw_id
                    if self._without_fragment(url) == exact_url:
                        exact_target_ids.append(raw_id)

        if len(exact_target_ids) > 1:
            return self._select_duplicate_exact_page(
                websocket,
                exact_url=exact_url,
                target_ids=exact_target_ids,
            )

        target_id = (
            exact_target_ids[0] if exact_target_ids else fallback_target_id
        )
        if not target_id:
            raise BrowserTransportError(
                "登录浏览器没有保持当前站点页面；请重新登录并保持窗口打开"
            )
        attached = self._command(
            websocket,
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = attached.get("sessionId", "")
        if not isinstance(session_id, str) or not session_id:
            raise BrowserTransportError("无法附加到登录浏览器页面")
        return target_id, session_id, bool(exact_target_ids)

    def _select_duplicate_exact_page(
        self,
        websocket: _SocketLike,
        *,
        exact_url: str,
        target_ids: list[str],
    ) -> tuple[str, str, bool]:
        """Choose the current useful tab among duplicate exact-URL pages.

        Merely attaching does not activate, navigate, or reload a page. Each
        candidate is verified against its root frame and ranked using only
        fixed boolean probes. A clean focused/visible page wins over an older
        challenge tab; candidate identifiers and probe failures are never
        included in an exception or log message.
        """

        attached_sessions: list[tuple[str, str]] = []
        selected: tuple[str, str] | None = None
        selected_rank: tuple[int, int, int, int, int, int] | None = None
        try:
            for target_id in target_ids:
                try:
                    attached = self._command(
                        websocket,
                        "Target.attachToTarget",
                        {"targetId": target_id, "flatten": True},
                    )
                except BrowserTransportError:
                    continue
                session_id = attached.get("sessionId", "")
                if not isinstance(session_id, str) or not session_id:
                    continue
                attached_sessions.append((target_id, session_id))

                rank = self._rank_exact_page_candidate(
                    websocket,
                    session_id=session_id,
                    exact_url=exact_url,
                )
                if rank is None:
                    continue
                if selected_rank is None or rank > selected_rank:
                    selected = (target_id, session_id)
                    selected_rank = rank

            if selected is None:
                raise BrowserTransportError(
                    "登录浏览器没有可读取的精确目标页面；请保持已验证页面打开"
                )

            for _target_id, session_id in attached_sessions:
                if session_id == selected[1]:
                    continue
                self._detach_quietly(websocket, session_id)
            return selected[0], selected[1], True
        except Exception:
            for _target_id, session_id in attached_sessions:
                self._detach_quietly(websocket, session_id)
            raise

    def _rank_exact_page_candidate(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
        exact_url: str,
    ) -> tuple[int, int, int, int, int, int] | None:
        """Return a content-free rank, or ``None`` for a stale target."""

        try:
            tree_result = self._command(
                websocket,
                "Page.getResourceTree",
                session_id=session_id,
            )
        except BrowserTransportError:
            return None
        frame_tree = tree_result.get("frameTree")
        frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
        frame_url = frame.get("url") if isinstance(frame, dict) else None
        if (
            not isinstance(frame_url, str)
            or not self.handles(frame_url)
            or self._without_fragment(frame_url) != exact_url
        ):
            return None

        try:
            evaluated = self._command(
                websocket,
                "Runtime.evaluate",
                {
                    "expression": _EXACT_PAGE_SELECTION_PROBE,
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        except BrowserTransportError:
            # The root frame is still an exact candidate. Keep it as a bounded
            # last resort if every richer probe is unavailable.
            return (0, 0, 0, 0, 0, 0)
        if evaluated.get("exceptionDetails") is not None:
            return (0, 0, 0, 0, 0, 0)
        remote = evaluated.get("result")
        value = remote.get("value") if isinstance(remote, dict) else None
        if not isinstance(value, dict):
            return (0, 0, 0, 0, 0, 0)

        visible = value.get("visible") is True
        focused = value.get("focused") is True
        challenge_value = value.get("challenge")
        challenge_known = isinstance(challenge_value, bool)
        clean = challenge_value is False
        # Prefer the conjunction the user actually cares about, then degrade
        # safely: clean+focused, clean+visible, clean, focus, visibility.
        return (
            int(clean and focused),
            int(clean and visible),
            int(clean),
            int(focused),
            int(visible),
            int(challenge_known),
        )

    def _detach_quietly(
        self,
        websocket: _SocketLike,
        session_id: str,
    ) -> None:
        try:
            self._command(
                websocket,
                "Target.detachFromTarget",
                {"sessionId": session_id},
            )
        except Exception:
            # Cleanup must neither replace the useful result nor expose a CDP
            # capability or target/session identifier in user-visible errors.
            pass

    def _read_current_document(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
        url: str,
        max_bytes: int,
        destination: io.BytesIO | Any,
    ) -> BrowserResource | None:
        """Return the already-loaded main document, or ``None`` to fetch it.

        Chromium may reject ``Page.getResourceContent`` for a main document on
        some versions or while a navigation is settling.  Those bounded CDP
        failures intentionally fall back to ``Network.loadNetworkResource``.
        A size-limit failure never falls back because doing so would issue a
        second request and could not make the resource safe to accept.
        """

        try:
            tree_result = self._command(
                websocket,
                "Page.getResourceTree",
                session_id=session_id,
            )
        except BrowserTransportError:
            return None
        frame_tree = tree_result.get("frameTree")
        frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
        if not isinstance(frame, dict):
            return None
        frame_id = frame.get("id")
        frame_url = frame.get("url")
        if (
            not isinstance(frame_id, str)
            or not frame_id
            or not isinstance(frame_url, str)
            or not self.handles(frame_url)
            or self._without_fragment(frame_url) != url
        ):
            return None
        try:
            content_result = self._command(
                websocket,
                "Page.getResourceContent",
                {"frameId": frame_id, "url": url},
                session_id=session_id,
            )
        except BrowserTransportError:
            return None
        content = content_result.get("content")
        encoded = content_result.get("base64Encoded")
        if not isinstance(content, str) or not isinstance(encoded, bool):
            return None

        if encoded:
            # Reject obviously over-limit values before allocating decoded
            # bytes.  CDP emits canonical base64 without whitespace.
            max_encoded = ((max_bytes + 2) // 3) * 4
            if len(content) > max_encoded:
                raise BrowserTransportError(
                    f"浏览器资源超过大小上限: > {max_bytes}"
                )
            try:
                payload = base64.b64decode(content, validate=True)
            except (ValueError, UnicodeError):
                return None
        else:
            if len(content) > max_bytes:
                raise BrowserTransportError(
                    f"浏览器资源超过大小上限: > {max_bytes}"
                )
            try:
                payload = content.encode("utf-8")
            except UnicodeError:
                return None
        if len(payload) > max_bytes:
            raise BrowserTransportError(
                f"浏览器资源超过大小上限: > {max_bytes}"
            )

        mime_type = frame.get("mimeType")
        headers: dict[str, str] = {}
        if isinstance(mime_type, str) and mime_type:
            safe_mime_type = mime_type.replace("\r", "").replace("\n", "")[:200]
            if safe_mime_type:
                headers["content-type"] = safe_mime_type
        if payload:
            destination.write(payload)
        return BrowserResource(
            url=url,
            status=200,
            headers=headers,
            size=len(payload),
        )

    def _read_rendered_dom(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
        url: str,
        max_bytes: int,
    ) -> bytes | None:
        """Read a bounded rendered DOM from the verified exact main frame.

        The JavaScript expression is a fixed constant: neither a URL nor any
        caller-controlled value is interpolated into executable code. Failure
        or an over-limit DOM simply omits the optional analysis channel; it
        must never replace or prevent capture of the original response bytes.
        """

        try:
            tree_result = self._command(
                websocket,
                "Page.getResourceTree",
                session_id=session_id,
            )
        except BrowserTransportError:
            return None
        frame_tree = tree_result.get("frameTree")
        frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
        if not isinstance(frame, dict):
            return None
        frame_url = frame.get("url")
        if (
            not isinstance(frame_url, str)
            or not self.handles(frame_url)
            or self._without_fragment(frame_url) != url
        ):
            return None
        try:
            evaluated = self._command(
                websocket,
                "Runtime.evaluate",
                {
                    "expression": "document.documentElement.outerHTML",
                    "returnByValue": True,
                },
                session_id=session_id,
            )
        except BrowserTransportError:
            return None
        if evaluated.get("exceptionDetails") is not None:
            return None
        remote = evaluated.get("result")
        value = remote.get("value") if isinstance(remote, dict) else None
        if (
            not isinstance(remote, dict)
            or remote.get("type") != "string"
            or not isinstance(value, str)
            or len(value) > max_bytes
        ):
            return None
        try:
            payload = value.encode("utf-8")
        except UnicodeError:
            return None
        return payload if len(payload) <= max_bytes else None

    def _settle_exact_page_once(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
        url: str,
    ) -> None:
        """Wait for one already-exact target without navigating or reloading it.

        The currently visible page may be the result of a CAPTCHA or other
        human verification step.  Reloading it would discard that approved
        state and can immediately trigger the challenge again.  ``url`` is
        never interpolated into JavaScript; only fixed, read-only expressions
        are evaluated.  A failed first attempt is remembered so later crawler
        phases cannot unexpectedly retry the page action.
        """

        if url in self._exact_action_failed:
            raise BrowserTransportError("登录浏览器目标页面此前加载失败")
        if url in self._exact_action_attempted:
            return
        self._exact_action_attempted.add(url)

        try:
            self._wait_for_page_settle(
                websocket,
                session_id=session_id,
                url=url,
            )
        except BrowserTransportError as exc:
            self._exact_action_failed.add(url)
            raise BrowserTransportError(
                f"登录浏览器目标页面读取失败：{exc}"
            ) from exc

    def _navigate_fallback_to_exact_once(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
        url: str,
    ) -> None:
        """Navigate one verified same-origin fallback tab to the exact URL."""

        if url in self._exact_action_failed:
            raise BrowserTransportError("登录浏览器目标页面此前加载失败")
        if url in self._exact_action_attempted:
            # The only expected subsequent state is an exact target selected by
            # Target.getTargets.  Never navigate a fallback twice if the user or
            # site moved it after the first action.
            raise BrowserTransportError(
                "登录浏览器未保持精确目标页面；已禁止重复导航"
            )
        self._exact_action_attempted.add(url)

        try:
            frame_result = self._command(
                websocket,
                "Page.getFrameTree",
                session_id=session_id,
            )
            frame_tree = frame_result.get("frameTree")
            frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
            frame_id = frame.get("id") if isinstance(frame, dict) else None
            frame_url = frame.get("url") if isinstance(frame, dict) else None
            if not isinstance(frame_id, str) or not frame_id:
                raise BrowserTransportError("同源备用页面缺少主框架")
            if not isinstance(frame_url, str) or not self.handles(frame_url):
                raise BrowserTransportError(
                    "备用页面已离开登录站点；已禁止导航"
                )

            navigated = self._command(
                websocket,
                "Page.navigate",
                {"url": url, "frameId": frame_id},
                session_id=session_id,
            )
            error_text = navigated.get("errorText")
            if isinstance(error_text, str) and error_text.strip():
                raise BrowserTransportError(
                    f"页面导航返回错误：{error_text.strip()[:200]}"
                )
            navigated_frame_id = navigated.get("frameId")
            if (
                not isinstance(navigated_frame_id, str)
                or not navigated_frame_id
                or navigated_frame_id != frame_id
            ):
                raise BrowserTransportError("页面导航未返回精确主框架")

            self._wait_for_page_settle(
                websocket,
                session_id=session_id,
                url=url,
            )
        except BrowserTransportError as exc:
            self._exact_action_failed.add(url)
            raise BrowserTransportError(
                f"登录浏览器目标页面导航失败：{exc}"
            ) from exc

    def _wait_for_page_settle(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
        url: str,
    ) -> None:
        deadline = self._clock() + min(self.timeout_seconds, 15.0)
        last_evaluate_error: BrowserTransportError | None = None
        while True:
            try:
                evaluated = self._command(
                    websocket,
                    "Runtime.evaluate",
                    {
                        "expression": "document.readyState",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                last_evaluate_error = None
                if evaluated.get("exceptionDetails") is not None:
                    raise BrowserTransportError("页面就绪状态读取失败")
                remote = evaluated.get("result")
                ready_state = (
                    remote.get("value") if isinstance(remote, dict) else None
                )
                if (
                    isinstance(remote, dict)
                    and remote.get("type") == "string"
                    and ready_state == "complete"
                ):
                    break
            except BrowserTransportError as exc:
                # The default execution context can disappear briefly while a
                # reload or navigation commits. Retry only this fixed read.
                last_evaluate_error = exc

            remaining = deadline - self._clock()
            if remaining <= 0:
                detail = (
                    f"：{last_evaluate_error}"
                    if last_evaluate_error is not None
                    else ""
                )
                raise BrowserTransportError(f"页面加载等待超时{detail}")
            self._sleeper(min(0.1, remaining))

        self._wait_for_dom_stability(
            websocket,
            session_id=session_id,
        )

        # A login, WAF, or cross-origin redirect is not the exact page the user
        # approved. Verify after asynchronous rendering before reading bytes.
        tree_result = self._command(
            websocket,
            "Page.getResourceTree",
            session_id=session_id,
        )
        frame_tree = tree_result.get("frameTree")
        frame = frame_tree.get("frame") if isinstance(frame_tree, dict) else None
        frame_url = frame.get("url") if isinstance(frame, dict) else None
        if (
            not isinstance(frame_url, str)
            or not self.handles(frame_url)
            or self._without_fragment(frame_url) != url
        ):
            raise BrowserTransportError("页面加载后离开了精确目标地址")

    def _wait_for_dom_stability(
        self,
        websocket: _SocketLike,
        *,
        session_id: str,
    ) -> None:
        """Wait briefly for asynchronous rendering after readyState completes.

        Some sites report ``complete`` before their application has populated
        the notice body.  Observe only the numeric length produced by one fixed
        expression; no page content, URL, keyword, or caller value is executed
        or returned to the crawler.  Three equal non-zero samples are required.
        """

        started = self._clock()
        # The normal browser timeout is 35 seconds.  Keep this post-load phase
        # independently bounded to twelve seconds while guaranteeing enough
        # room for the mandatory two-second grace period and stable samples,
        # even if an unusually small network timeout was configured.
        deadline = started + min(max(self.timeout_seconds, 3.0), 12.0)
        grace = min(2.0, max(0.0, deadline - self._clock()))
        if grace:
            self._sleeper(grace)

        previous_length: int | None = None
        equal_samples = 0
        last_evaluate_error: BrowserTransportError | None = None
        while True:
            if self._clock() >= deadline:
                detail = (
                    f"：{last_evaluate_error}"
                    if last_evaluate_error is not None
                    else ""
                )
                raise BrowserTransportError(f"DOM 稳定等待超时{detail}")
            try:
                evaluated = self._command(
                    websocket,
                    "Runtime.evaluate",
                    {
                        "expression": "document.documentElement.outerHTML.length",
                        "returnByValue": True,
                    },
                    session_id=session_id,
                )
                if evaluated.get("exceptionDetails") is not None:
                    raise BrowserTransportError("DOM 长度读取失败")
                remote = evaluated.get("result")
                value = remote.get("value") if isinstance(remote, dict) else None
                if (
                    isinstance(remote, dict)
                    and remote.get("type") == "number"
                    and isinstance(value, int)
                    and not isinstance(value, bool)
                    and value > 0
                ):
                    if value == previous_length:
                        equal_samples += 1
                    else:
                        previous_length = value
                        equal_samples = 1
                    if equal_samples >= 3:
                        return
                else:
                    previous_length = None
                    equal_samples = 0
                last_evaluate_error = None
            except BrowserTransportError as exc:
                # Navigation can replace the execution context briefly even
                # after readyState was observed.  Retry only the same fixed,
                # read-only expression within the hard deadline.
                previous_length = None
                equal_samples = 0
                last_evaluate_error = exc

            remaining = deadline - self._clock()
            if remaining <= 0:
                continue
            self._sleeper(min(0.5, remaining))

    @staticmethod
    def _root_frame_id(frame_tree: object) -> str:
        if not isinstance(frame_tree, dict):
            return ""
        tree = frame_tree.get("frameTree")
        frame = tree.get("frame") if isinstance(tree, dict) else None
        frame_id = frame.get("id") if isinstance(frame, dict) else None
        return frame_id if isinstance(frame_id, str) else ""

    def _load(
        self,
        url: str,
        *,
        max_bytes: int,
        destination: io.BytesIO | Any,
        include_analysis: bool,
        refresh_exact: bool,
    ) -> BrowserResource:
        if not self.handles(url):
            raise BrowserTransportError("浏览器会话只允许请求登录站点的精确来源")
        if max_bytes < 0:
            raise BrowserTransportError("资源大小上限无效")
        parsed = urlsplit(url)
        clean_url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
        websocket: _SocketLike | None = None
        session_id = ""
        stream = ""
        total = 0
        analysis_body: bytes | None = None
        try:
            websocket = self._socket_factory(self.websocket_url, self.timeout_seconds)
            _, session_id, selected_exact = self._select_page(
                websocket,
                exact_url=clean_url,
            )
            if refresh_exact and not selected_exact:
                self._navigate_fallback_to_exact_once(
                    websocket,
                    session_id=session_id,
                    url=clean_url,
                )
                selected_exact = True
            if selected_exact:
                if refresh_exact:
                    self._settle_exact_page_once(
                        websocket,
                        session_id=session_id,
                        url=clean_url,
                    )
                if include_analysis:
                    analysis_body = self._read_rendered_dom(
                        websocket,
                        session_id=session_id,
                        url=clean_url,
                        max_bytes=max_bytes,
                    )
                current = self._read_current_document(
                    websocket,
                    session_id=session_id,
                    url=clean_url,
                    max_bytes=max_bytes,
                    destination=destination,
                )
                if current is not None:
                    return BrowserResource(
                        url=current.url,
                        status=current.status,
                        headers=current.headers,
                        size=current.size,
                        analysis_body=analysis_body,
                    )
            frame_result = self._command(
                websocket, "Page.getFrameTree", session_id=session_id
            )
            frame_id = self._root_frame_id(frame_result)
            params: dict[str, Any] = {
                "url": clean_url,
                "options": {"disableCache": False, "includeCredentials": True},
            }
            if frame_id:
                params["frameId"] = frame_id
            loaded = self._command(
                websocket,
                "Network.loadNetworkResource",
                params,
                session_id=session_id,
            )
            resource = loaded.get("resource")
            if not isinstance(resource, dict):
                raise BrowserTransportError("浏览器未返回资源描述")
            status = resource.get("httpStatusCode", 0)
            if isinstance(status, bool) or not isinstance(status, (int, float)):
                status = 0
            status = int(status)
            if not resource.get("success", False):
                error_name = str(resource.get("netErrorName", ""))[:200]
                raise BrowserTransportError(
                    f"浏览器资源加载失败{f'：{error_name}' if error_name else ''}"
                )
            raw_headers = resource.get("headers", {})
            headers = {
                str(key).lower(): str(value)
                for key, value in raw_headers.items()
            } if isinstance(raw_headers, dict) else {}
            stream_value = resource.get("stream", "")
            stream = stream_value if isinstance(stream_value, str) else ""
            if stream:
                while True:
                    chunk = self._command(
                        websocket,
                        "IO.read",
                        {"handle": stream, "size": 512 * 1024},
                        session_id=session_id,
                    )
                    data = chunk.get("data", "")
                    if not isinstance(data, str):
                        raise BrowserTransportError("浏览器资源数据格式无效")
                    try:
                        payload = (
                            base64.b64decode(data, validate=True)
                            if chunk.get("base64Encoded", False)
                            else data.encode("utf-8")
                        )
                    except (ValueError, UnicodeError) as exc:
                        raise BrowserTransportError("浏览器资源数据解码失败") from exc
                    total += len(payload)
                    if total > max_bytes:
                        raise BrowserTransportError(
                            f"浏览器资源超过大小上限: > {max_bytes}"
                        )
                    if payload:
                        destination.write(payload)
                    if chunk.get("eof", False):
                        break
            return BrowserResource(
                url=clean_url,
                status=status or 200,
                headers=headers,
                size=total,
                analysis_body=analysis_body,
            )
        except BrowserTransportError:
            raise
        except Exception as exc:
            raise BrowserTransportError(
                "无法连接已登录浏览器；请保持登录窗口打开"
            ) from exc
        finally:
            if websocket is not None and stream:
                try:
                    self._command(
                        websocket,
                        "IO.close",
                        {"handle": stream},
                        session_id=session_id or None,
                    )
                except Exception:
                    pass
            if websocket is not None and session_id:
                try:
                    self._command(
                        websocket,
                        "Target.detachFromTarget",
                        {"sessionId": session_id},
                    )
                except Exception:
                    pass
            if websocket is not None:
                websocket.close()

    def request(self, url: str, *, max_bytes: int) -> BrowserResource:
        buffer = io.BytesIO()
        result = self._load(
            url,
            max_bytes=max_bytes,
            destination=buffer,
            include_analysis=True,
            refresh_exact=True,
        )
        return BrowserResource(
            url=result.url,
            status=result.status,
            headers=result.headers,
            body=buffer.getvalue(),
            size=result.size,
            analysis_body=result.analysis_body,
        )

    def download_to(self, url: str, path: Path, *, max_bytes: int) -> BrowserResource:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("wb") as output:
                result = self._load(
                    url,
                    max_bytes=max_bytes,
                    destination=output,
                    include_analysis=False,
                    refresh_exact=False,
                )
                output.flush()
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return result

    def close(self) -> None:
        # The visual-login manager owns Browser.close and profile deletion.
        return
