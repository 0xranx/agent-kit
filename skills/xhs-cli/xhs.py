"""
小红书运营 CLI 工具
统一入口：搜索、笔记详情、评论、用户信息、未读通知。

用法:
  python xhs.py search "关键词"
  python xhs.py detail <note_id> <xsec_token>
  python xhs.py comments <note_id> <xsec_token>
  python xhs.py note <note_id> <xsec_token>       # 详情 + 评论
  python xhs.py me                                  # 当前用户
  python xhs.py unread                              # 未读通知
"""

import os
import sys
import json
from pathlib import Path

# 配置：环境变量 > config.yaml > 默认值
def _load_cfg():
    cfg = {}
    cfg_path = Path(__file__).parent / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml
            cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        except ImportError:
            for line in cfg_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip().strip('"').strip("'")
                    if v:
                        cfg[k.strip()] = v
    return {
        "account_name": os.environ.get("XHS_ACCOUNT_NAME", cfg.get("account_name", "")),
        "search_keyword": os.environ.get("XHS_SEARCH_KEYWORD", cfg.get("search_keyword", "")),
    }

_CFG = _load_cfg()

from xhs_client import (
    search_notes,
    get_note_detail,
    get_comments,
    get_note_with_comments,
    get_unread_count,
    get_user_info,
    get_user_posted_notes,
    check_response,
)
from xhs_sign import browser_navigate_and_capture


def _ts_to_str(ts) -> str:
    if not ts:
        return "未知"
    import datetime
    try:
        return datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(ts)


def fmt_search(data: dict) -> str:
    if not check_response(data):
        return "搜索失败"
    items = data.get("data", {}).get("items", [])
    if not items:
        return "没有搜索结果"
    lines = [f"搜索到 {len(items)} 条结果:\n"]
    for i, item in enumerate(items, 1):
        nc = item.get("note_card", {})
        title = nc.get("display_title", "(无标题)")
        user = nc.get("user", {}).get("nickname", "未知")
        likes = nc.get("interact_info", {}).get("liked_count", "0")
        nid = item.get("id", "") or nc.get("note_id", "")
        token = item.get("xsec_token", "") or nc.get("xsec_token", "")
        ntype = "视频" if nc.get("type") == "video" else "图文"
        lines.append(f"  {i}. [{ntype}] {title}")
        lines.append(f"     作者: {user} | 赞: {likes}")
        lines.append(f"     ID: {nid}")
        lines.append(f"     token: {token}")
        lines.append("")
    return "\n".join(lines)


def fmt_detail(data: dict) -> str:
    if not check_response(data):
        return "获取失败"
    note = data.get("data", {})
    if not note or not note.get("title"):
        return "笔记数据为空"
    user = note.get("user", {})
    interact = note.get("interactInfo", {})
    tags = [t.get("name", "") for t in note.get("tagList", [])]
    images = note.get("imageList", [])
    lines = [
        f"标题: {note.get('title', '?')}",
        f"作者: {user.get('nickname', '?')} (ID: {user.get('userId', '?')})",
        f"描述: {note.get('desc', '(无)')[:300]}",
        f"点赞: {interact.get('likedCount', '?')} | 收藏: {interact.get('collectedCount', '?')} | "
        f"评论: {interact.get('commentCount', '?')} | 分享: {interact.get('shareCount', '?')}",
        f"标签: {', '.join(tags) if tags else '无'}",
        f"图片: {len(images)} 张",
        f"发布: {_ts_to_str(note.get('time'))} | IP: {note.get('ipLocation', '未知')}",
    ]
    return "\n".join(lines)


def fmt_comments(data: dict) -> str:
    if not check_response(data):
        return "获取失败"
    cdata = data.get("data", {})
    comments = cdata.get("comments", [])
    has_more = cdata.get("has_more", False)
    if not comments:
        return "暂无评论"
    lines = [f"评论 ({len(comments)} 条, {'还有更多' if has_more else '全部'}):\n"]
    for i, c in enumerate(comments, 1):
        ui = c.get("user_info", {})
        nickname = ui.get("nickname", "匿名")
        uid = ui.get("user_id", "")
        content = c.get("content", "").replace("\n", " ")
        likes = c.get("like_count", "0")
        subs = c.get("sub_comment_count", 0)
        ts = _ts_to_str(c.get("create_time"))
        ip = c.get("ip_location", "")
        lines.append(f"  {i}. [{nickname}] {content}")
        lines.append(f"     uid: {uid} | 赞: {likes} | 回复: {subs} | {ts} | {ip}")
        for sc in c.get("sub_comments", [])[:2]:
            sc_ui = sc.get("user_info", {})
            lines.append(f"       ↳ [{sc_ui.get('nickname', '?')}] (uid:{sc_ui.get('user_id', '?')}) {sc.get('content', '')[:60]}")
        lines.append("")
    return "\n".join(lines)


def fmt_note_full(data: dict) -> str:
    if not check_response(data):
        return "获取失败"
    page = data.get("data", {})
    detail_data = {"code": 0, "data": page.get("note", {})}
    comment_data = {
        "code": 0,
        "data": {
            "comments": page.get("comments", []),
            "has_more": page.get("comment_has_more", False),
        },
    }
    return fmt_detail(detail_data) + "\n\n" + fmt_comments(comment_data)


def fmt_user_profile(data: dict) -> str:
    """格式化用户主页信息"""
    if not check_response(data):
        return "获取失败"
    user = data.get("data", {}).get("user", {})
    if not user:
        return "用户数据为空"
    interact = data.get("data", {}).get("interact", {})
    lines = [
        f"昵称: {user.get('nickname', '?')}",
        f"用户ID: {user.get('user_id', '?')}",
        f"红薯号: {user.get('red_id', '?')}",
        f"简介: {user.get('desc', '(无)')[:200]}",
        f"IP: {user.get('ip_location', '未知')}",
        f"关注: {interact.get('follows', '?')} | 粉丝: {interact.get('fans', '?')} | "
        f"获赞与收藏: {interact.get('interaction', '?')}",
    ]
    tags = user.get("tags", [])
    if tags:
        lines.append(f"标签: {', '.join(t.get('name', '') for t in tags)}")
    return "\n".join(lines)


def fmt_stats(detail_results: list[dict]) -> str:
    """格式化账号帖子数据追踪（从笔记详情列表）"""
    lines = ["账号帖子数据追踪:\n"]
    lines.append(f"{'#':>3} | {'日期':10} | {'标题':30} | {'赞':>5} | {'藏':>5} | {'评':>5} | {'分享':>5}")
    lines.append("-" * 85)

    total_likes = total_collects = total_comments = total_shares = 0
    for i, d in enumerate(detail_results, 1):
        note = d.get("data", {})
        if not note:
            continue
        title = (note.get("title") or "(无标题)")[:28]
        interact = note.get("interactInfo", {})
        likes = interact.get("likedCount", "0")
        collects = interact.get("collectedCount", "0")
        comments = interact.get("commentCount", "0")
        shares = interact.get("shareCount", "0")
        ts = _ts_to_str(note.get("time"))[:10]

        total_likes += int(str(likes).replace(",", "") or 0)
        total_collects += int(str(collects).replace(",", "") or 0)
        total_comments += int(str(comments).replace(",", "") or 0)
        total_shares += int(str(shares).replace(",", "") or 0)

        lines.append(f"{i:3} | {ts:10} | {title:30} | {likes:>5} | {collects:>5} | {comments:>5} | {shares:>5}")

    lines.append("-" * 85)
    lines.append(f"{'合计':16} | {'':30} | {total_likes:>5} | {total_collects:>5} | {total_comments:>5} | {total_shares:>5}")
    return "\n".join(lines)


def fmt_stats_quick(notes: list[dict]) -> str:
    """格式化账号帖子数据追踪（快速模式，仅用列表数据，只有点赞数）"""
    lines = ["账号帖子数据追踪 (快速模式):\n"]
    lines.append(f"{'#':>3} | {'标题':40} | {'类型':4} | {'赞':>6}")
    lines.append("-" * 65)

    total_likes = 0
    for i, note in enumerate(notes, 1):
        title = (note.get("display_title") or "(无标题)")[:38]
        ntype = "视频" if note.get("type") == "video" else "图文"
        ii = note.get("interact_info", {})
        likes = ii.get("likedCount") or ii.get("liked_count") or "0"
        total_likes += int(str(likes).replace(",", "") or 0)
        lines.append(f"{i:3} | {title:40} | {ntype:4} | {likes:>6}")

    lines.append("-" * 65)
    lines.append(f"{'合计':47} | {total_likes:>6}")
    lines.append(f"\n共 {len(notes)} 篇帖子")
    return "\n".join(lines)


USAGE = """小红书运营 CLI

用法:
  python xhs.py search <关键词>                搜索笔记
  python xhs.py detail <ID> <token>            笔记详情
  python xhs.py comments <ID> <token> [--all]  评论列表（--all 获取全部评论）
  python xhs.py note <ID> <token>              详情 + 评论
  python xhs.py user <user_id>                 查看用户主页
  python xhs.py stats [--detail]                账号所有帖子数据追踪（--detail 含收藏/评论/分享）
  python xhs.py me                             当前用户
  python xhs.py unread                         未读通知

提示: <token> 即搜索结果中的 xsec_token"""


def _get_user_profile(user_id: str) -> dict:
    """通过 user_id 获取用户主页信息（SSR 提取 + API 捕获双保险）"""
    import re as _re
    from xhs_sign import _ensure_browser
    import xhs_sign

    _ensure_browser()
    page = xhs_sign._page
    url = f"https://www.xiaohongshu.com/user/profile/{user_id}"

    api_result = []

    def _on_response(response):
        if "/api/sns/web/v1/user/otherinfo" in response.url and response.status == 200:
            try:
                api_result.append(response.json())
            except Exception:
                pass

    page.on("response", _on_response)
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        import time; time.sleep(3)
    except Exception:
        pass
    finally:
        page.remove_listener("response", _on_response)

    if api_result:
        return api_result[0]

    html = page.content()
    match = _re.search(
        r'window\.__INITIAL_STATE__\s*=\s*({.*?})\s*</script>', html, _re.DOTALL
    )
    if match:
        raw = match.group(1).replace(":undefined", ":null")
        state = json.loads(raw)
        user_page = state.get("user", {}).get("userPageData", {})
        if user_page:
            basic = user_page.get("basicInfo", {})
            interactions = user_page.get("interactions", [])
            fans = follows = interaction = "?"
            for item in interactions:
                if item.get("type") == "fans":
                    fans = item.get("count", "?")
                elif item.get("type") == "follows":
                    follows = item.get("count", "?")
                elif item.get("type") == "interaction":
                    interaction = item.get("count", "?")
            return {
                "code": 0,
                "data": {
                    "user": {
                        "nickname": basic.get("nickname", "?"),
                        "user_id": user_id,
                        "red_id": basic.get("redId", "?"),
                        "desc": basic.get("desc", ""),
                        "ip_location": basic.get("ipLocation", ""),
                        "tags": basic.get("tags", []),
                    },
                    "interact": {
                        "fans": fans,
                        "follows": follows,
                        "interaction": interaction,
                    },
                },
            }

    return {"code": -1, "msg": "未获取到用户信息", "data": {}}


def main():
    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "search":
        if len(sys.argv) < 3:
            print("用法: python xhs.py search <关键词>")
            sys.exit(1)
        print(fmt_search(search_notes(sys.argv[2])))

    elif cmd == "detail":
        if len(sys.argv) < 4:
            print("用法: python xhs.py detail <note_id> <xsec_token>")
            sys.exit(1)
        print(fmt_detail(get_note_detail(sys.argv[2], sys.argv[3])))

    elif cmd == "comments":
        if len(sys.argv) < 4:
            print("用法: python xhs.py comments <note_id> <xsec_token> [--all]")
            sys.exit(1)
        fetch_all = "--all" in sys.argv
        max_pages = 0 if fetch_all else 1
        print(fmt_comments(get_comments(sys.argv[2], sys.argv[3],
                                        max_pages=max_pages)))

    elif cmd == "note":
        if len(sys.argv) < 4:
            print("用法: python xhs.py note <note_id> <xsec_token>")
            sys.exit(1)
        print(fmt_note_full(get_note_with_comments(sys.argv[2], sys.argv[3])))

    elif cmd == "user":
        if len(sys.argv) < 3:
            print("用法: python xhs.py user <user_id>")
            sys.exit(1)
        print(fmt_user_profile(_get_user_profile(sys.argv[2])))

    elif cmd == "stats":
        from xhs_client import clear_note_cache
        detail_mode = "--detail" in sys.argv

        _account_name = _CFG["account_name"]
        _account_keyword = _CFG["search_keyword"]

        print("正在获取用户信息...")
        me = get_user_info()
        if not check_response(me):
            print("获取用户信息失败，请检查登录状态")
            sys.exit(1)
        user_id = me["data"]["user_id"]
        print(f"用户: {me['data'].get('nickname', '?')} ({user_id})")

        print("正在获取帖子列表...")
        posted = get_user_posted_notes(
            user_id,
            account_name=_account_name,
            search_keyword=_account_keyword,
        )
        source = posted.get("data", {}).get("source", "?")
        notes = posted.get("data", {}).get("notes", [])
        if not notes:
            print("未获取到帖子")
            sys.exit(1)
        print(f"获取到 {len(notes)} 篇帖子 (来源: {source})")

        if not detail_mode:
            print(fmt_stats_quick(notes))
            print("\n提示: 加 --detail 参数可获取完整数据（收藏、评论、分享，但更慢）")
        else:
            print("正在逐篇获取详细数据...")
            details = []
            for note in notes:
                nid = note.get("note_id", "")
                token = note.get("xsec_token", "")
                if nid and token:
                    clear_note_cache(nid)
                    detail = get_note_detail(nid, token)
                    if check_response(detail):
                        details.append(detail)
                    else:
                        print(f"  跳过: {note.get('display_title', nid)[:20]}")
            details.sort(key=lambda d: d.get("data", {}).get("time", 0))
            print(fmt_stats(details))

    elif cmd == "me":
        r = get_user_info()
        if check_response(r):
            d = r["data"]
            print(f"昵称: {d.get('nickname')}")
            print(f"红薯号: {d.get('red_id')}")
            print(f"用户ID: {d.get('user_id')}")
            print(f"简介: {d.get('desc') or '(无)'}")
        else:
            print("获取用户信息失败")

    elif cmd == "unread":
        r = get_unread_count()
        if check_response(r):
            d = r["data"]
            print(f"未读总数: {d.get('unread_count', 0)}")
            print(f"点赞: {d.get('likes', 0)} | 关注: {d.get('connections', 0)} | @我: {d.get('mentions', 0)}")
        else:
            print("获取未读通知失败")

    else:
        print(f"未知命令: {cmd}")
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
