"""
抖音 Web 浏览器管理模块（基于 agent-browser）

核心策略：
- 使用 --user-data-dir 持久化浏览器数据（cookie/session/localStorage 自动保留）
- 用 headed 模式（抖音对 headless 检测严格）
- 验证码/登录弹窗由用户在浏览器中手动处理
- 每次操作等待目标内容出现，不主动检测验证码

依赖: npm install -g agent-browser && agent-browser install
"""

import json
import subprocess
import time
from pathlib import Path
from urllib.parse import quote, unquote

SKILL_DIR = Path(__file__).parent
DATA_DIR = SKILL_DIR / "data"
BROWSER_DATA_DIR = DATA_DIR / "browser_profile"  # 持久化浏览器数据
COOKIE_FILE = DATA_DIR / "douyin_cookie.txt"      # 备用 cookie 文件

_browser_open = False


def _p(msg: str):
    print(msg, flush=True)


def _run(cmd: str, timeout: int = 15) -> str:
    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return ""
    except Exception as e:
        return f"error: {e}"


# ── 浏览器管理 ───────────────────────────────────

def _ensure_browser():
    """确保浏览器已打开。使用持久化 profile 复用登录态。"""
    global _browser_open
    if _browser_open:
        return

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 用 --user-data-dir 持久化所有浏览器数据（cookie/session/localStorage）
    _run(
        f'agent-browser open "https://www.douyin.com" '
        f'--headed --session-name douyin_session',
        timeout=30
    )
    time.sleep(3)
    _browser_open = True


def _close_browser():
    global _browser_open
    _run("agent-browser close")
    _browser_open = False


def _wait_for_content(selector: str, label: str = "内容", max_wait: int = 180) -> bool:
    """等待页面上出现指定元素（用户可在浏览器中处理验证码/登录）。"""
    _p(f"等待{label}加载（如有验证码或登录弹窗，请在浏览器中完成）...")
    for i in range(max_wait // 2):
        time.sleep(2)
        count = _run(f'agent-browser get count "{selector}"', timeout=5)
        if count and count.isdigit() and int(count) > 0:
            _p(f"  {label}已加载")
            return True
        if i % 10 == 9:
            _p(f"  等待中... ({(i+1)*2}秒)")
    _p(f"  {label}加载超时")
    return False


def _navigate(url: str, timeout: int = 30):
    """导航到 URL。"""
    _ensure_browser()
    _run(f'agent-browser goto "{url}"', timeout=timeout)
    time.sleep(3)


def _eval_js(script: str, timeout: int = 15):
    """在页面中执行 JavaScript 并返回结果。"""
    tmp = DATA_DIR / "_eval_tmp.js"
    tmp.write_text(script, encoding="utf-8")
    raw = _run(f'agent-browser eval "$(cat \'{tmp}\')"', timeout=timeout)
    tmp.unlink(missing_ok=True)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


# ── 搜索 ────────────────────────────────────────

def search_videos(keyword: str) -> dict:
    """搜索抖音视频。"""
    _navigate(f"https://www.douyin.com/search/{quote(keyword)}?type=video")

    # 等待视频链接出现
    if not _wait_for_content('a[href*="/video/"]', "搜索结果"):
        return {"aweme_list": [], "msg": "未获取到搜索结果"}

    time.sleep(2)

    # 从 DOM 提取
    result = _eval_js('''
        (() => {
            const links = document.querySelectorAll('a[href*="/video/"]');
            const results = [];
            const seen = new Set();
            links.forEach(a => {
                const href = a.getAttribute("href") || "";
                const match = href.match(/\\/video\\/(\\d+)/);
                if (!match || seen.has(match[1])) return;
                seen.add(match[1]);
                const text = (a.textContent || "").trim();
                const parts = text.match(/^([\\d:]+)?\\s*([\\.\\d万]+)?\\s*(.+?)\\s*@(.+?)\\s*(\\d+.*前|\\d+小时前)?$/s);
                results.push({
                    aweme_id: match[1],
                    desc: parts ? parts[3].trim() : text.slice(0, 100),
                    author: {nickname: parts ? parts[4].trim() : ""},
                    statistics: {digg_count: parts ? parts[2] : "0"},
                });
            });
            return JSON.stringify({aweme_list: results, has_more: 1});
        })()
    ''')

    if isinstance(result, dict) and result.get("aweme_list"):
        return result
    return {"aweme_list": [], "msg": "未获取到搜索结果"}


def search_users(keyword: str) -> dict:
    """搜索抖音用户。"""
    _navigate(f"https://www.douyin.com/search/{quote(keyword)}?type=user")

    if not _wait_for_content('a[href*="/user/"]', "用户列表"):
        return {"data": [], "msg": "未获取到用户"}

    time.sleep(2)
    result = _eval_js('''
        (() => {
            const el = document.getElementById("RENDER_DATA");
            if (!el) return JSON.stringify({data: []});
            const data = JSON.parse(decodeURIComponent(el.textContent));
            for (const [k, v] of Object.entries(data)) {
                if (v && v.user_list) return JSON.stringify({data: v.user_list});
            }
            return JSON.stringify({data: []});
        })()
    ''')

    if isinstance(result, dict) and result.get("data"):
        return result
    return {"data": [], "msg": "未获取到用户"}


# ── 视频详情 ─────────────────────────────────────

def get_video_detail(aweme_id: str) -> dict:
    """获取视频详情。"""
    _navigate(f"https://www.douyin.com/video/{aweme_id}")

    if not _wait_for_content('#RENDER_DATA', "视频详情"):
        return {"aweme_detail": None, "msg": "获取视频详情失败"}

    time.sleep(2)
    result = _eval_js('''
        (() => {
            const el = document.getElementById("RENDER_DATA");
            if (!el) return JSON.stringify({error: "no RENDER_DATA"});
            const data = JSON.parse(decodeURIComponent(el.textContent));
            for (const [k, v] of Object.entries(data)) {
                if (v && v.aweme && v.aweme.detail) {
                    return JSON.stringify({aweme_detail: v.aweme.detail});
                }
            }
            return JSON.stringify({error: "no detail"});
        })()
    ''')

    if isinstance(result, dict) and result.get("aweme_detail"):
        return result
    return {"aweme_detail": None, "msg": "获取视频详情失败"}


# ── 评论 ─────────────────────────────────────────

def get_comments(aweme_id: str) -> dict:
    """获取视频评论。"""
    current_url = _run("agent-browser get url", timeout=5)
    if aweme_id not in (current_url or ""):
        _navigate(f"https://www.douyin.com/video/{aweme_id}")
        _wait_for_content('#RENDER_DATA', "视频页面")
        time.sleep(3)

    # 滚动触发评论加载
    _run('agent-browser scroll --direction down --amount 500')
    time.sleep(3)

    result = _eval_js('''
        (() => {
            const items = document.querySelectorAll(
                '[class*="CommentListContainer"] [class*="commentItem"],' +
                '[class*="comment-item"],' +
                '[class*="comment-mainContent"]'
            );
            if (!items.length) return JSON.stringify({comments: []});
            const comments = [];
            items.forEach(item => {
                const nameEl = item.querySelector('[class*="name"], a[href*="/user/"]');
                const contentEl = item.querySelector('[class*="content"], p');
                const likeEl = item.querySelector('[class*="like"], [class*="digg"]');
                if (contentEl) {
                    comments.push({
                        text: contentEl.textContent.trim().slice(0, 200),
                        user: {nickname: nameEl ? nameEl.textContent.trim() : ''},
                        digg_count: likeEl ? likeEl.textContent.trim() : '0',
                    });
                }
            });
            return JSON.stringify({comments, has_more: 1});
        })()
    ''')

    if isinstance(result, dict) and result.get("comments"):
        return result
    return {"comments": [], "msg": "未获取到评论"}


# ── 用户主页 ─────────────────────────────────────

def get_user_profile(sec_user_id: str) -> dict:
    """获取用户主页信息。"""
    _navigate(f"https://www.douyin.com/user/{sec_user_id}")

    if not _wait_for_content('#RENDER_DATA', "用户主页"):
        return {"user": None, "msg": "获取用户信息失败"}

    time.sleep(2)
    result = _eval_js('''
        (() => {
            const el = document.getElementById("RENDER_DATA");
            if (!el) return JSON.stringify({error: "no"});
            const data = JSON.parse(decodeURIComponent(el.textContent));
            for (const [k, v] of Object.entries(data)) {
                if (v && v.user && v.user.user) return JSON.stringify({user: v.user.user});
                if (v && v.user && v.user.secUid) return JSON.stringify({user: v.user});
            }
            return JSON.stringify({error: "no user"});
        })()
    ''')

    if isinstance(result, dict) and result.get("user"):
        return result
    return {"user": None, "msg": "获取用户信息失败"}


def get_user_posts(sec_user_id: str) -> dict:
    """获取用户作品列表。"""
    current_url = _run("agent-browser get url", timeout=5)
    if sec_user_id not in (current_url or ""):
        _navigate(f"https://www.douyin.com/user/{sec_user_id}")
        _wait_for_content('#RENDER_DATA', "用户主页")
        time.sleep(3)

    result = _eval_js('''
        (() => {
            const el = document.getElementById("RENDER_DATA");
            if (!el) return JSON.stringify({error: "no"});
            const data = JSON.parse(decodeURIComponent(el.textContent));
            for (const [k, v] of Object.entries(data)) {
                if (v && v.post && v.post.data) {
                    return JSON.stringify({aweme_list: v.post.data, has_more: v.post.hasMore || 0});
                }
            }
            return JSON.stringify({error: "no posts"});
        })()
    ''')

    if isinstance(result, dict) and result.get("aweme_list"):
        return result
    return {"aweme_list": [], "msg": "获取用户作品失败"}


# ── 登录 ─────────────────────────────────────────

def login_interactive(timeout: int = 300):
    """打开浏览器让用户完成所有验证和登录。使用持久化 profile 保存状态。"""
    _run("agent-browser close", timeout=5)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    _p("正在打开抖音...")
    _run(
        f'agent-browser open "https://www.douyin.com" '
        f'--headed --session-name douyin_session',
        timeout=30
    )
    time.sleep(3)

    _p(f"请在浏览器中完成所有验证和登录（{timeout // 60} 分钟内）：")
    _p("  1. 如果有拼图/形状验证码 → 手动完成")
    _p("  2. 如果要求手机号验证 → 输入并验证")
    _p("  3. 如果有登录弹窗 → 扫码或手机号登录")
    _p("  4. 完成后你会看到抖音首页推荐内容")
    _p("")
    _p("脚本会自动检测登录状态...")

    for i in range(timeout // 3):
        time.sleep(3)
        output = _run("agent-browser storage cookie get", timeout=5)
        try:
            cookies = json.loads(output)
            names = {c["name"] for c in cookies if isinstance(c, dict)}
            if "sessionid" in names or "sessionid_ss" in names:
                time.sleep(3)  # 多等一会让 session 完全建立
                _p("检测到登录成功！")
                _p("浏览器 profile 已保存，下次打开会自动恢复登录态。")
                # 同时备份 cookie 到文件
                pairs = {c["name"]: c["value"] for c in cookies if isinstance(c, dict)}
                COOKIE_FILE.write_text("; ".join(f"{k}={v}" for k, v in pairs.items()))
                _p(f"Cookie 备份: {COOKIE_FILE}")
                _run("agent-browser close", timeout=5)
                return
        except (json.JSONDecodeError, TypeError):
            pass
        if i % 10 == 9:
            _p(f"  等待中... ({(i+1)*3}秒)")

    _p("等待超时")
    _run("agent-browser close", timeout=5)


def save_cookie_string(cookie_str: str) -> None:
    """手动保存 Cookie 字符串。"""
    cookie_str = cookie_str.strip()
    if not cookie_str:
        _p("Cookie 不能为空")
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    COOKIE_FILE.write_text(cookie_str)
    cookies = [p.strip().split("=", 1) for p in cookie_str.split(";") if "=" in p]
    _p(f"Cookie 已保存（{len(cookies)} 项）")


def _has_valid_cookie() -> bool:
    # 优先检查浏览器 profile 是否存在
    if BROWSER_DATA_DIR.exists() and any(BROWSER_DATA_DIR.iterdir()):
        return True
    if COOKIE_FILE.exists() and COOKIE_FILE.read_text().strip():
        return True
    return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "login":
        login_interactive()
    elif len(sys.argv) > 2 and sys.argv[1] == "set-cookie":
        save_cookie_string(sys.argv[2])
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        if BROWSER_DATA_DIR.exists() and any(BROWSER_DATA_DIR.iterdir()):
            print("已有浏览器 profile（登录态应已保存）")
        elif _has_valid_cookie():
            print("有 Cookie 文件")
        else:
            print("未登录")
    elif len(sys.argv) > 1 and sys.argv[1] == "screenshot":
        _ensure_browser()
        out = sys.argv[2] if len(sys.argv) > 2 else "/tmp/douyin_screenshot.png"
        _run(f"agent-browser screenshot --annotate {out}")
        print(f"截图: {out}")
    else:
        print("用法:")
        print("  python douyin_sign.py login              # 登录（验证码+扫码）")
        print('  python douyin_sign.py set-cookie "..."    # 手动粘贴 Cookie')
        print("  python douyin_sign.py status             # 检查登录状态")
        print("  python douyin_sign.py screenshot [path]  # 截图")
