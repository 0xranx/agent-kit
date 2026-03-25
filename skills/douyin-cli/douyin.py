"""
抖音 CLI 工具
统一入口：搜索、视频详情、评论、用户信息。

用法:
  python douyin.py search "关键词"               # 搜索视频
  python douyin.py search "关键词" --user         # 搜索用户
  python douyin.py detail <aweme_id>              # 视频详情
  python douyin.py comments <aweme_id>            # 评论列表
  python douyin.py video <aweme_id>               # 详情 + 评论
  python douyin.py user <sec_user_id>             # 用户主页
  python douyin.py posts <sec_user_id>            # 用户作品列表
  python douyin.py login                          # 扫码登录
  python douyin.py set-cookie "..."               # 手动粘贴 Cookie
  python douyin.py status                         # 检查登录状态
"""

import sys
import json
import datetime

from douyin_sign import (
    search_videos,
    search_users,
    get_video_detail,
    get_comments,
    get_user_profile,
    get_user_posts,
    login_interactive,
    save_cookie_string,
    scroll_more,
    set_auto_connect,
    _has_valid_cookie,
    COOKIE_FILE,
    BROWSER_DATA_DIR,
)


def _ts_to_str(ts) -> str:
    if not ts:
        return "未知"
    try:
        return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, TypeError):
        return "未知"


def _count_str(n) -> str:
    if n is None:
        return "0"
    s = str(n).strip()
    if not s:
        return "0"
    # 已经是 "4.0万" 这样的格式，直接返回
    if "万" in s or "w" in s.lower():
        return s
    try:
        num = int(float(s))
        if num >= 10000:
            return f"{num / 10000:.1f}w"
        return str(num)
    except (ValueError, TypeError):
        return s


# ── 格式化 ───────────────────────────────────────

def fmt_search(data: dict) -> str:
    # 新版 API 返回 aweme_list，旧版返回 data[].aweme_info
    items = data.get("aweme_list") or data.get("data", [])
    if not items:
        return f"没有搜索结果 ({data.get('msg', '')})"

    results = []
    for item in items:
        if item.get("aweme_id"):
            # 新版：item 本身就是 aweme 对象
            results.append(item)
        else:
            # 旧版：item.aweme_info
            aweme = item.get("aweme_info")
            if not aweme:
                mix = item.get("aweme_mix_info", {}).get("mix_items", [])
                if mix:
                    aweme = mix[0]
            if aweme:
                results.append(aweme)

    if not results:
        return "没有搜索结果"

    lines = [f"搜索到 {len(results)} 条结果:\n"]
    for i, aweme in enumerate(results, 1):
        desc = (aweme.get("desc") or "无描述")[:60]
        author = aweme.get("author", {})
        nickname = author.get("nickname", "未知")
        stats = aweme.get("statistics", {})
        likes = _count_str(stats.get("digg_count", 0))
        comments = _count_str(stats.get("comment_count", 0))
        aweme_id = aweme.get("aweme_id", "")

        lines.append(f"  {i}. {desc}")
        lines.append(f"     作者: {nickname} | 赞: {likes} | 评论: {comments}")
        lines.append(f"     ID: {aweme_id}")
        lines.append("")

    return "\n".join(lines)


def fmt_search_users(data: dict) -> str:
    items = data.get("data", [])
    if not items:
        return "没有搜索结果"

    users = []
    for item in items:
        user_info = item.get("user_list", [{}])
        if isinstance(user_info, list) and user_info:
            users.append(user_info[0].get("user_info", {}))
        elif item.get("user_info"):
            users.append(item["user_info"])

    if not users:
        return "没有搜索结果"

    lines = [f"搜索到 {len(users)} 个用户:\n"]
    for i, u in enumerate(users, 1):
        nickname = u.get("nickname", "未知")
        sec_uid = u.get("sec_uid", "")
        signature = (u.get("signature") or "无简介")[:50]
        follower = _count_str(u.get("follower_count", 0))
        lines.append(f"  {i}. {nickname} | 粉丝: {follower}")
        lines.append(f"     简介: {signature}")
        lines.append(f"     sec_uid: {sec_uid}")
        lines.append("")

    return "\n".join(lines)


def fmt_detail(data: dict) -> str:
    aweme = data.get("aweme_detail")
    if not aweme:
        return f"获取失败 ({data.get('msg', '')})"

    author = aweme.get("author", {})
    stats = aweme.get("statistics", {})
    desc = aweme.get("desc", "无描述")
    create_time = _ts_to_str(aweme.get("create_time"))

    lines = [
        f"描述: {desc}",
        f"作者: {author.get('nickname', '?')} (sec_uid: {author.get('sec_uid', '?')[:20]}...)",
        f"赞: {_count_str(stats.get('digg_count'))} | "
        f"评论: {_count_str(stats.get('comment_count'))} | "
        f"收藏: {_count_str(stats.get('collect_count'))} | "
        f"分享: {_count_str(stats.get('share_count'))}",
        f"发布: {create_time}",
        f"ID: {aweme.get('aweme_id', '?')}",
    ]
    return "\n".join(lines)


def fmt_comments(data: dict) -> str:
    comments = data.get("comments", [])
    if not comments:
        return f"暂无评论 ({data.get('msg', '')})"

    has_more = data.get("has_more", 0)
    lines = [f"评论 ({len(comments)} 条, {'还有更多' if has_more else '全部'}):\n"]

    for i, c in enumerate(comments, 1):
        user = c.get("user", {})
        nickname = user.get("nickname", "匿名")
        uid = user.get("sec_uid", "")
        text = (c.get("text") or "").replace("\n", " ")
        likes = _count_str(c.get("digg_count", 0))
        replies = c.get("reply_comment_total", 0)
        ts = _ts_to_str(c.get("create_time"))
        ip = c.get("ip_label", "")

        lines.append(f"  {i}. [{nickname}] {text}")
        lines.append(f"     赞: {likes} | 回复: {replies} | {ts} | {ip}")
        lines.append("")

    return "\n".join(lines)


def fmt_user(data: dict) -> str:
    user = data.get("user")
    if not user:
        return f"获取失败 ({data.get('msg', '')})"

    lines = [
        f"昵称: {user.get('nickname', '?')}",
        f"抖音号: {user.get('unique_id') or user.get('short_id') or '未设置'}",
        f"sec_uid: {user.get('sec_uid', '?')}",
        f"简介: {(user.get('signature') or '无')[:100]}",
        f"IP: {user.get('ip_location', '未知')}",
        f"关注: {_count_str(user.get('following_count'))} | "
        f"粉丝: {_count_str(user.get('follower_count'))} | "
        f"获赞: {_count_str(user.get('total_favorited'))}",
        f"作品: {_count_str(user.get('aweme_count'))}",
    ]
    return "\n".join(lines)


def fmt_posts(data: dict) -> str:
    posts = data.get("aweme_list", [])
    if not posts:
        return f"暂无作品 ({data.get('msg', '')})"

    has_more = data.get("has_more", 0)
    lines = [f"作品列表 ({len(posts)} 条, {'还有更多' if has_more else '全部'}):\n"]

    for i, aweme in enumerate(posts, 1):
        desc = (aweme.get("desc") or "无描述")[:50]
        stats = aweme.get("statistics", {})
        likes = _count_str(stats.get("digg_count", 0))
        comments = _count_str(stats.get("comment_count", 0))
        ts = _ts_to_str(aweme.get("create_time"))
        aweme_id = aweme.get("aweme_id", "")

        lines.append(f"  {i}. {desc}")
        lines.append(f"     赞: {likes} | 评论: {comments} | {ts}")
        lines.append(f"     ID: {aweme_id}")
        lines.append("")

    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────

HELP = """用法:
  python douyin.py search <关键词>               搜索视频
  python douyin.py search <关键词> --user         搜索用户
  python douyin.py detail <aweme_id>              视频详情
  python douyin.py comments <aweme_id>            评论列表
  python douyin.py video <aweme_id>               详情 + 评论
  python douyin.py user <sec_user_id>             用户主页
  python douyin.py posts <sec_user_id>            用户作品
  python douyin.py more [次数]                    滚动加载更多（默认3次）
  python douyin.py export <文件路径> [--csv]       导出上次搜索结果
  python douyin.py login                          扫码登录
  python douyin.py set-cookie "..."               手动粘贴 Cookie
  python douyin.py status                         检查登录状态

选项:
  --auto-connect    连接用户已登录的 Chrome（绕过验证码，推荐）
"""


_last_search_result = None  # 缓存上次搜索结果供 export 使用


def main():
    global _last_search_result

    args = sys.argv[1:]
    if not args:
        print(HELP)
        return

    # 全局选项：--auto-connect
    if "--auto-connect" in args:
        set_auto_connect(True)
        args = [a for a in args if a != "--auto-connect"]

    if not args:
        print(HELP)
        return

    cmd = args[0]

    if cmd == "search" and len(args) >= 2:
        keyword = args[1]
        if "--user" in args:
            data = search_users(keyword)
            _last_search_result = data
            print(fmt_search_users(data))
        else:
            data = search_videos(keyword)
            _last_search_result = data
            print(fmt_search(data))

    elif cmd == "detail" and len(args) >= 2:
        print(fmt_detail(get_video_detail(args[1])))

    elif cmd == "comments" and len(args) >= 2:
        print(fmt_comments(get_comments(args[1])))

    elif cmd == "video" and len(args) >= 2:
        detail = get_video_detail(args[1])
        comments = get_comments(args[1])
        print(fmt_detail(detail))
        print()
        print(fmt_comments(comments))

    elif cmd == "user" and len(args) >= 2:
        print(fmt_user(get_user_profile(args[1])))

    elif cmd == "posts" and len(args) >= 2:
        data = get_user_posts(args[1])
        _last_search_result = data
        print(fmt_posts(data))

    elif cmd == "more":
        n = int(args[1]) if len(args) > 1 else 3
        print(f"正在滚动加载更多（{n} 次）...")
        scroll_more(n)
        print("加载完成。重新执行 search/posts 命令可获取更多结果。")

    elif cmd == "export" and len(args) >= 2:
        filepath = args[1]
        fmt = "csv" if "--csv" in args else "json"
        if not _last_search_result:
            print("没有可导出的数据。请先执行 search 或 posts 命令。")
            return
        _export_data(_last_search_result, filepath, fmt)

    elif cmd == "login":
        login_interactive()

    elif cmd == "set-cookie" and len(args) >= 2:
        save_cookie_string(args[1])

    elif cmd == "status":
        if BROWSER_DATA_DIR.exists() and any(BROWSER_DATA_DIR.iterdir()):
            print("已有浏览器 profile（登录态已保存）")
        elif _has_valid_cookie():
            print("有 Cookie 文件")
        else:
            print("未登录")

    else:
        print(HELP)


def _export_data(data: dict, filepath: str, fmt: str = "json"):
    """导出搜索/作品结果到文件。"""
    import csv
    import os

    # 提取列表数据
    items = data.get("aweme_list") or data.get("data", [])
    if not items:
        print("数据为空，无法导出。")
        return

    # 标准化为扁平结构
    rows = []
    for item in items:
        aweme = item
        if item.get("aweme_info"):
            aweme = item["aweme_info"]
        author = aweme.get("author", {})
        stats = aweme.get("statistics", {})
        rows.append({
            "aweme_id": aweme.get("aweme_id", ""),
            "desc": (aweme.get("desc") or "")[:200],
            "author": author.get("nickname", ""),
            "sec_uid": author.get("sec_uid", ""),
            "likes": stats.get("digg_count", 0),
            "comments": stats.get("comment_count", 0),
            "shares": stats.get("share_count", 0),
            "create_time": aweme.get("create_time", ""),
        })

    if not rows:
        print("无法解析数据结构。")
        return

    if not os.path.isabs(filepath):
        filepath = os.path.join(os.getcwd(), filepath)

    if fmt == "csv":
        with open(filepath, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
    else:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(rows, f, ensure_ascii=False, indent=2)

    print(f"已导出 {len(rows)} 条数据到 {filepath}")


if __name__ == "__main__":
    main()
