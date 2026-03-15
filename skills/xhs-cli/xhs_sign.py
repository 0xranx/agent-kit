"""
小红书 Web API 签名模块
通过 Playwright 无头浏览器调用 window._webmsxyw() 生成 x-s / x-t 签名。
采用懒加载 + 单例模式，首次调用时启动浏览器，后续复用。
"""

import atexit
import json
import time
from pathlib import Path

_playwright = None
_browser = None
_context = None
_page = None
_a1 = ""

SKILL_DIR = Path(__file__).parent
COOKIE_FILE = SKILL_DIR / "data" / "xhs_cookie.txt"


def _p(msg: str):
    """打印并立即刷新"""
    print(msg, flush=True)


def _parse_cookie_string(cookie_str: str) -> list[dict]:
    """将 cookie 字符串解析为 Playwright 格式的 cookie 列表"""
    cookies = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookies.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".xiaohongshu.com",
                "path": "/",
            })
    return cookies


def _ensure_browser():
    """懒加载：首次调用时启动 Playwright 浏览器并导航到小红书"""
    global _playwright, _browser, _context, _page, _a1

    if _page is not None:
        return

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=False)
    _context = _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    )

    if COOKIE_FILE.exists():
        cookie_str = COOKIE_FILE.read_text().strip()
        if cookie_str:
            _context.add_cookies(_parse_cookie_string(cookie_str))

    _page = _context.new_page()
    stealth = Stealth()
    stealth.apply_stealth_sync(_page)

    _page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")
    time.sleep(3)
    _page.reload()
    time.sleep(2)

    for cookie in _context.cookies():
        if cookie["name"] == "a1":
            _a1 = cookie["value"]
            break

    atexit.register(_shutdown)


def _shutdown():
    global _playwright, _browser, _context, _page
    try:
        if _browser:
            _browser.close()
        if _playwright:
            _playwright.stop()
    except Exception:
        pass
    _page = None
    _context = None
    _browser = None
    _playwright = None


def sign(api_path: str, data: dict | str | None = None) -> dict:
    """
    为指定 API 路径生成签名头。

    Args:
        api_path: API 路径，如 "/api/sns/web/v1/search/notes"
        data: POST body（dict 会被序列化为 JSON 字符串，GET 请求传 None 或 ""）

    Returns:
        {"x-s": "...", "x-t": "..."}
    """
    _ensure_browser()

    if data is None:
        data_str = ""
    elif isinstance(data, dict):
        data_str = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    else:
        data_str = str(data)

    result = _page.evaluate(
        "([url, data]) => window._webmsxyw(url, data)",
        [api_path, data_str],
    )

    return {
        "x-s": result["X-s"],
        "x-t": str(result["X-t"]),
    }


def get_a1() -> str:
    """返回浏览器中的 a1 cookie 值"""
    _ensure_browser()
    return _a1


def get_cookie_string() -> str:
    """返回浏览器中所有 cookie 拼成的字符串（含 httpOnly），用于 HTTP 请求头"""
    _ensure_browser()
    cookies = _context.cookies()
    seen = {}
    for c in cookies:
        seen[c["name"]] = c["value"]
    return "; ".join(f"{k}={v}" for k, v in seen.items())


def browser_navigate_and_capture(url: str, api_pattern: str,
                                 timeout: int = 15) -> dict | None:
    """
    导航到指定 URL，拦截匹配 api_pattern 的第一个 API 响应并返回 JSON。
    利用页面自身的 AJAX 请求（含完整签名和指纹），不做任何手动 fetch。
    """
    _ensure_browser()

    captured = []

    def _on_response(response):
        if api_pattern in response.url and response.status == 200:
            try:
                captured.append(response.json())
            except Exception:
                pass

    _page.on("response", _on_response)
    try:
        _page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        time.sleep(2)
    except Exception:
        pass
    finally:
        _page.remove_listener("response", _on_response)

    return captured[0] if captured else None


def navigate_search_page(keyword: str, timeout: int = 20) -> dict:
    """
    导航到搜索页，从 Vue 运行时状态提取搜索结果。
    小红书搜索已改为 SSR + Vue hydration，不再发客户端 API 请求。

    Returns:
        {"items": [...], "has_more": bool} 或 {"code": -1, "msg": "..."}
    """
    from urllib.parse import quote as _quote

    _ensure_browser()
    url = f"https://www.xiaohongshu.com/search_result?keyword={_quote(keyword)}"

    try:
        _page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        time.sleep(3)
    except Exception:
        pass

    try:
        feeds = _page.evaluate('''() => {
            const s = window.__INITIAL_STATE__;
            if (!s || !s.search) return null;
            const raw = s.search.feeds._rawValue || s.search.feeds._value || s.search.feeds;
            if (!Array.isArray(raw)) return null;
            const hasMore = s.search.hasMore;
            const hasMoreVal = hasMore && (hasMore._rawValue !== undefined ? hasMore._rawValue : hasMore);
            return {
                items: raw.map(item => {
                    const nc = item.noteCard || {};
                    return {
                        id: item.id || nc.noteId || '',
                        model_type: item.modelType || '',
                        note_card: {
                            note_id: nc.noteId || item.id || '',
                            xsec_token: item.xsecToken || nc.xsecToken || '',
                            display_title: nc.displayTitle || '',
                            type: nc.type || 'normal',
                            interact_info: {
                                liked_count: (nc.interactInfo || {}).likedCount || '0',
                                collected_count: (nc.interactInfo || {}).collectedCount || '0',
                                comment_count: (nc.interactInfo || {}).commentCount || '0',
                                share_count: (nc.interactInfo || {}).shareCount || '0',
                            },
                            user: {
                                nickname: (nc.user || {}).nickname || '',
                                user_id: (nc.user || {}).userId || '',
                                avatar: (nc.user || {}).avatar || '',
                            },
                        },
                    };
                }),
                has_more: !!hasMoreVal,
            };
        }''')

        if feeds and feeds.get("items"):
            return {"code": 0, "success": True, "data": feeds}
        return {"code": -1, "msg": "搜索结果为空", "data": {"items": []}}
    except Exception as e:
        return {"code": -1, "msg": f"提取搜索结果失败: {e}", "data": {"items": []}}


def navigate_note_page(note_id: str, xsec_token: str = "",
                       timeout: int = 20) -> dict:
    """
    导航到笔记页面，从 SSR 提取笔记详情，同时拦截评论 API 响应。
    需要 xsec_token（来自搜索结果），否则会被安全重定向到 404。

    Returns:
        {"note": {...}, "comments": [...], "comment_has_more": bool, "comment_cursor": str}
    """
    import re as _re
    from urllib.parse import quote as _quote

    _ensure_browser()

    url = f"https://www.xiaohongshu.com/explore/{note_id}"
    if xsec_token:
        url += f"?xsec_token={_quote(xsec_token)}&xsec_source=pc_search"

    comment_data = []

    def _on_response(response):
        if "/api/sns/web/v2/comment/page" in response.url and response.status == 200:
            try:
                comment_data.append(response.json())
            except Exception:
                pass

    _page.on("response", _on_response)
    try:
        _page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        time.sleep(3)
    except Exception:
        pass
    finally:
        _page.remove_listener("response", _on_response)

    if "404" in _page.url:
        return {"note": None, "comments": [],
                "error": "页面被安全重定向到 404，可能缺少 xsec_token"}

    note = {}
    html = _page.content()
    match = _re.search(
        r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', html, _re.DOTALL
    )
    if match:
        raw = match.group(1).replace(":undefined", ":null")
        state = json.loads(raw)
        detail_map = state.get("note", {}).get("noteDetailMap", {})
        for v in detail_map.values():
            n = v.get("note", {})
            if n:
                note = n
                break

    comments = []
    has_more = False
    cursor = ""
    if comment_data:
        cdata = comment_data[0].get("data", {})
        comments = cdata.get("comments", [])
        has_more = cdata.get("has_more", False)
        cursor = cdata.get("cursor", "")

    return {
        "note": note or None,
        "comments": comments,
        "comment_has_more": has_more,
        "comment_cursor": cursor,
    }


def navigate_user_posted(user_id: str, timeout: int = 20) -> list[dict]:
    """
    导航到用户主页，从 SSR __INITIAL_STATE__ 提取所有已发布笔记。
    不需要 xsec_token，只需 user_id。
    如果遇到验证码/重定向，返回空列表（调用方应降级到搜索方式）。

    Returns:
        list of note dicts, each with keys:
        note_id, xsec_token, display_title, type, interact_info, cover, user
    """
    import re as _re
    _ensure_browser()

    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"
    try:
        _page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
        time.sleep(3)
    except Exception:
        pass

    if "captcha" in _page.url or "login" in _page.url:
        _p(f"[navigate_user_posted] 触发验证码，将降级到搜索方式")
        _page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded",
                    timeout=10000)
        time.sleep(1)
        return []

    html = _page.content()
    match = _re.search(
        r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', html, _re.DOTALL
    )
    if not match:
        _p(f"[navigate_user_posted] 未找到 SSR 数据")
        return []

    raw = match.group(1).replace(":undefined", ":null")
    state = json.loads(raw)

    notes_tabs = state.get("user", {}).get("notes", [])
    if not notes_tabs or not isinstance(notes_tabs[0], list):
        return []

    results = []
    for item in notes_tabs[0]:
        if not isinstance(item, dict):
            continue
        nc = item.get("noteCard", {})
        if not nc.get("noteId"):
            continue
        results.append({
            "note_id": nc["noteId"],
            "xsec_token": nc.get("xsecToken", ""),
            "display_title": nc.get("displayTitle", ""),
            "type": nc.get("type", "normal"),
            "interact_info": nc.get("interactInfo", {}),
            "cover": nc.get("cover", {}),
            "user": nc.get("user", {}),
        })

    return results


def set_cookies(cookie_str: str):
    """将外部 cookie 字符串注入到浏览器上下文中"""
    _ensure_browser()
    cookies_to_add = []
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            name, value = part.split("=", 1)
            cookies_to_add.append({
                "name": name.strip(),
                "value": value.strip(),
                "domain": ".xiaohongshu.com",
                "path": "/",
            })
    if cookies_to_add:
        _context.add_cookies(cookies_to_add)
        _page.reload()
        time.sleep(2)


def login_interactive(timeout: int = 120):
    """打开可见浏览器窗口让用户扫码登录，自动检测登录成功后保存 cookie。

    Args:
        timeout: 最长等待秒数，默认 120 秒
    """
    global _playwright, _browser, _context, _page, _a1

    _shutdown()

    from playwright.sync_api import sync_playwright
    from playwright_stealth import Stealth

    _playwright = sync_playwright().start()
    _browser = _playwright.chromium.launch(headless=False)
    _context = _browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
    )
    _page = _context.new_page()
    stealth = Stealth()
    stealth.apply_stealth_sync(_page)

    _page.goto("https://www.xiaohongshu.com", wait_until="domcontentloaded")

    _p(f"浏览器已打开，请扫码登录小红书（{timeout} 秒内完成）。")
    _p("登录成功后会自动检测并保存 Cookie...")

    # 等登录弹窗出现
    login_modal = _page.locator('.login-modal')
    try:
        login_modal.wait_for(state="visible", timeout=15000)
        _p("登录弹窗已出现，请用小红书 App 扫码...")
    except Exception:
        _p("未检测到登录弹窗，页面结构可能已变更")

    # 等弹窗消失（扫码成功后 login-modal 会关闭）
    done = False
    for i in range(timeout // 2):
        time.sleep(2)
        try:
            # login-modal 消失 = 登录成功
            if not login_modal.is_visible():
                time.sleep(3)  # 等页面完成跳转
                done = True
                _p("检测到登录成功，正在保存 Cookie...")
                break
        except Exception:
            time.sleep(3)
            done = True
            _p("检测到登录成功，正在保存 Cookie...")
            break
        if i % 10 == 9:
            _p(f"  仍在等待扫码... ({(i + 1) * 2}秒)")

    if not done:
        _p("等待超时。关闭浏览器。")
        _browser.close()
        _playwright.stop()
        _browser = _page = _context = _playwright = None
        return

    for cookie in _context.cookies():
        if cookie["name"] == "a1":
            _a1 = cookie["value"]
            break

    cookie_str = get_cookie_string()
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(cookie_str)
    _p(f"Cookie 已保存到 {COOKIE_FILE}")

    _browser.close()
    _browser = _page = _context = None
    _playwright.stop()
    _playwright = None


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login_interactive()
    else:
        print("正在启动签名服务...")
        headers = sign("/api/sns/web/v1/search/notes", {"keyword": "AI编程"})
        print(f"签名结果: {headers}")
        print(f"a1 cookie: {get_a1()}")
