"""
小红书 Web API 客户端
- 搜索：Playwright 导航搜索页 + 拦截 API 响应
- 笔记详情 + 评论：带 xsec_token 导航笔记页，SSR 提取详情 + 拦截评论 API
- 简单接口（unread_count 等）：Playwright 签名 + requests 直接调用
"""

from urllib.parse import quote
from xhs_sign import (
    browser_navigate_and_capture,
    navigate_note_page,
    navigate_search_page,
    navigate_user_posted,
    sign,
)

import json
import time

_note_cache: dict[str, dict] = {}


def search_notes(keyword: str, page: int = 1, page_size: int = 20,
                 sort: str = "general", note_type: int = 0) -> dict:
    """搜索笔记：导航到搜索页，从 Vue 运行时状态提取结果"""
    return navigate_search_page(keyword)


def _load_note_page(note_id: str, xsec_token: str = "") -> dict:
    """内部：加载笔记页面并缓存结果（一次导航同时获取详情 + 评论）"""
    if note_id in _note_cache:
        return _note_cache[note_id]
    result = navigate_note_page(note_id, xsec_token)
    if result.get("note"):
        _note_cache[note_id] = result
    return result


def get_note_detail(note_id: str, xsec_token: str = "") -> dict:
    """
    获取笔记详情（标题、描述、作者、互动数据、标签、图片等）。
    xsec_token 来自搜索结果的 item['xsec_token']，缺少会导致 404。
    """
    page_data = _load_note_page(note_id, xsec_token)
    if page_data.get("error"):
        return {"code": -1, "msg": page_data["error"], "data": {}}
    note = page_data.get("note")
    if not note:
        return {"code": -1, "msg": "未提取到笔记数据", "data": {}}
    return {"code": 0, "msg": "成功", "data": note}


def get_comments(note_id: str, xsec_token: str = "",
                  max_pages: int = 1) -> dict:
    """
    获取笔记评论列表，支持分页。
    max_pages=1 只取第一页（默认），设为 0 取全部。
    """
    page_data = _load_note_page(note_id, xsec_token)
    if page_data.get("error"):
        return {"code": -1, "msg": page_data["error"], "data": {}}

    all_comments = list(page_data.get("comments", []))
    has_more = page_data.get("comment_has_more", False)
    cursor = page_data.get("comment_cursor", "")

    if has_more and (max_pages == 0 or max_pages > 1):
        extra = _fetch_more_comments_by_scroll(note_id, xsec_token)
        seen_ids = {c.get("id") for c in all_comments}
        for c in extra:
            if c.get("id") not in seen_ids:
                all_comments.append(c)
                seen_ids.add(c.get("id"))
        has_more = False

    return {
        "code": 0,
        "msg": "成功",
        "data": {
            "comments": all_comments,
            "has_more": has_more,
        },
    }


def _fetch_more_comments_by_scroll(note_id: str, xsec_token: str = "",
                                    max_scrolls: int = 50) -> list[dict]:
    """滚动笔记详情页的 note-scroller 容器来触发原生评论翻页"""
    import xhs_sign
    xhs_sign._ensure_browser()
    page = xhs_sign._page

    all_comment_pages = []

    def _on_response(response):
        if "/api/sns/web/v2/comment/page" in response.url and response.status == 200:
            try:
                all_comment_pages.append(response.json())
            except Exception:
                pass

    page.on("response", _on_response)
    try:
        stale_rounds = 0
        prev_page_count = 0
        for _ in range(max_scrolls):
            page.evaluate("""
                const el = document.querySelector('.note-scroller');
                if (el) el.scrollTop = el.scrollHeight;
            """)
            time.sleep(2)

            if len(all_comment_pages) == prev_page_count:
                stale_rounds += 1
                if stale_rounds >= 3:
                    break
            else:
                stale_rounds = 0
                prev_page_count = len(all_comment_pages)

            if all_comment_pages:
                latest = all_comment_pages[-1]
                if not latest.get("data", {}).get("has_more", False):
                    break
    finally:
        page.remove_listener("response", _on_response)

    merged = []
    for pg in all_comment_pages:
        merged.extend(pg.get("data", {}).get("comments", []))
    return merged


def get_note_with_comments(note_id: str, xsec_token: str = "") -> dict:
    """一次页面加载同时获取笔记详情和评论（推荐使用）"""
    page_data = _load_note_page(note_id, xsec_token)
    if page_data.get("error"):
        return {"code": -1, "msg": page_data["error"], "data": {}}
    return {"code": 0, "msg": "成功", "data": page_data}


def get_user_posted_notes(user_id: str, account_name: str = "",
                          search_keyword: str = "") -> dict:
    """
    获取指定用户已发布的所有笔记。
    主路径：通过用户主页 SSR 提取（快，含 xsec_token）。
    备选路径：通过搜索获取（遇到验证码时自动降级）。

    Args:
        user_id: 用户 ID
        account_name: 账号昵称（降级搜索时用于过滤结果）
        search_keyword: 搜索关键词（降级搜索时使用）

    Returns:
        {"code": 0, "data": {"notes": [...], "source": "profile"|"search"}}
    """
    notes = navigate_user_posted(user_id)
    if notes:
        return {"code": 0, "msg": "成功", "data": {"notes": notes, "source": "profile"}}

    if not search_keyword:
        return {"code": -1, "msg": "主页方式失败且未提供搜索关键词", "data": {"notes": []}}

    print(f"降级到搜索方式: {search_keyword}")
    search_data = search_notes(search_keyword)
    items = search_data.get("data", {}).get("items", [])
    if account_name:
        items = [it for it in items
                 if account_name in it.get("note_card", {}).get("user", {}).get("nickname", "")]

    notes = []
    for it in items:
        nc = it.get("note_card", {})
        notes.append({
            "note_id": it.get("id", ""),
            "xsec_token": it.get("xsec_token", ""),
            "display_title": nc.get("display_title", ""),
            "type": nc.get("type", "normal"),
            "interact_info": nc.get("interact_info", {}),
            "cover": nc.get("cover", {}),
            "user": nc.get("user", {}),
        })

    if not notes:
        return {"code": -1, "msg": "搜索未找到帖子", "data": {"notes": []}}
    return {"code": 0, "msg": "成功", "data": {"notes": notes, "source": "search"}}


def clear_note_cache(note_id: str = ""):
    """清除缓存（传空则清除所有）"""
    if note_id:
        _note_cache.pop(note_id, None)
    else:
        _note_cache.clear()


def get_unread_count() -> dict:
    """获取未读通知（这个接口简单，可以用签名方式直接调）"""
    import requests
    from xhs_sign import get_cookie_string
    headers = sign("/api/sns/web/unread_count", None)
    headers["Cookie"] = get_cookie_string()
    headers["User-Agent"] = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0.0.0 Safari/537.36")
    headers["Origin"] = "https://www.xiaohongshu.com"
    headers["Referer"] = "https://www.xiaohongshu.com/"
    resp = requests.get("https://edith.xiaohongshu.com/api/sns/web/unread_count",
                        headers=headers, timeout=10)
    return resp.json()


def get_user_info() -> dict:
    """获取当前登录用户信息"""
    import requests
    from xhs_sign import get_cookie_string
    headers = sign("/api/sns/web/v2/user/me", None)
    headers["Cookie"] = get_cookie_string()
    headers["User-Agent"] = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0.0.0 Safari/537.36")
    headers["Origin"] = "https://www.xiaohongshu.com"
    headers["Referer"] = "https://www.xiaohongshu.com/"
    resp = requests.get("https://edith.xiaohongshu.com/api/sns/web/v2/user/me",
                        headers=headers, timeout=10)
    return resp.json()


def check_response(data: dict) -> bool:
    """检查 API 响应是否成功"""
    code = data.get("code", data.get("result", {}).get("code", -1))
    if code == 0 or data.get("success"):
        return True
    msg = data.get("msg", data.get("result", {}).get("message", "unknown"))
    print(f"[API 错误] code={code}, msg={msg}")
    return False
