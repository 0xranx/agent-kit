#!/usr/bin/env python3
"""
飞书文档助手 — 统一 CLI
融合 feishu-docx（读取/导出）、feishu_sync（写入/同步）、块级 API（精确编辑）。

用法:
    python feishu_doc.py read <URL>                          # 读取文档→Markdown
    python feishu_doc.py read <URL> --with-block-ids         # 带 block_id
    python feishu_doc.py list-blocks <URL>                   # 列出所有块
    python feishu_doc.py create <title> -c "内容" [-f file]  # 创建文档
    python feishu_doc.py create <title> --wiki <parent>      # 创建到知识库
    python feishu_doc.py append <URL> -c "内容"              # 追加内容
    python feishu_doc.py overwrite <URL> -f file             # 清空重写
    python feishu_doc.py update-block <URL> <block_id> "txt" # 更新单块
    python feishu_doc.py delete-block <URL> <block_id>       # 删除单块
    python feishu_doc.py wiki-tree <space_id|URL>            # 知识库树
    python feishu_doc.py wiki-move <URL> <parent_node>       # 移入知识库
    python feishu_doc.py wiki-sync <md_path> --parent <node> # 同步到知识库
    python feishu_doc.py export-wiki <space_id|URL> -o dir   # 批量导出
    python feishu_doc.py import-wechat <URL>                 # 微信文章→飞书
    python feishu_doc.py notify "标题" "内容"                 # 群卡片消息
    python feishu_doc.py send "文本"                          # 群文本消息
    python feishu_doc.py read-chat [N]                       # 读群消息
    python feishu_doc.py login                               # OAuth 登录
    python feishu_doc.py test                                # 测试连通性

依赖: pip install feishu-docx markdown2feishu httpx pyyaml
"""

import asyncio
import http.server
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import webbrowser
from pathlib import Path
from typing import Any

import httpx

SKILL_DIR = Path(__file__).parent
CONFIG_PATH = SKILL_DIR / "config.yaml"
REGISTRY_PATH = SKILL_DIR / "sync_registry.json"
USER_TOKEN_PATH = SKILL_DIR / "user_token.json"

BASE = "https://open.feishu.cn/open-apis"

MAX_RETRIES = 3
BLOCKS_PER_BATCH = 50
MAX_TABLE_ROWS = 9
CELL_WRITE_DELAY = 0.3

# ── 配置加载 ─────────────────────────────────────

def _load_config() -> dict:
    """从 config.yaml 加载配置，环境变量优先。"""
    cfg = {}
    if CONFIG_PATH.exists():
        # 兼容无 pyyaml 环境：简单 key: "value" 解析
        try:
            import yaml
            cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
        except ImportError:
            for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.strip().strip('"').strip("'")
                    if v:
                        cfg[k.strip()] = v

    def get(key: str, default: str = "") -> str:
        env_key = f"FEISHU_{key.upper()}"
        return os.environ.get(env_key, str(cfg.get(key, default)))

    config = {
        "app_id": get("app_id"),
        "app_secret": get("app_secret"),
        "wiki_space_id": get("wiki_space_id"),
        "default_parent_node": get("default_parent_node"),
        "notify_chat_id": get("notify_chat_id"),
        "mode": get("mode", "auto"),
    }

    if not config["app_id"] or not config["app_secret"]:
        print("请配置飞书凭证: 设置环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET，或编辑 config.yaml")

    return config


CFG = _load_config()


# ── Token 管理 ───────────────────────────────────

_token_cache: dict[str, Any] = {"token": None, "expires_at": 0}


async def _get_tenant_token(client: httpx.AsyncClient) -> str:
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"]:
        return _token_cache["token"]

    resp = await client.post(f"{BASE}/auth/v3/tenant_access_token/internal", json={
        "app_id": CFG["app_id"], "app_secret": CFG["app_secret"],
    })
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败: {data}")
    _token_cache["token"] = data["tenant_access_token"]
    _token_cache["expires_at"] = now + 5400
    return _token_cache["token"]


async def _headers(client: httpx.AsyncClient) -> dict[str, str]:
    token = await _get_tenant_token(client)
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── User Token (OAuth) ──────────────────────────

def _load_user_token() -> dict | None:
    if USER_TOKEN_PATH.exists():
        data = json.loads(USER_TOKEN_PATH.read_text(encoding="utf-8"))
        if data.get("access_token"):
            return data
    return None


def _save_user_token(data: dict) -> None:
    USER_TOKEN_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def _get_user_token(client: httpx.AsyncClient) -> str:
    token_data = _load_user_token()
    if not token_data:
        raise RuntimeError("未登录，请先运行: python feishu_doc.py login")
    if time.time() < token_data.get("expires_at", 0) - 60:
        return token_data["access_token"]
    # 刷新
    refresh = token_data.get("refresh_token")
    if not refresh:
        raise RuntimeError("Token 已过期，请重新 login")
    app_resp = await client.post(f"{BASE}/auth/v3/app_access_token/internal", json={
        "app_id": CFG["app_id"], "app_secret": CFG["app_secret"],
    })
    app_token = app_resp.json()["app_access_token"]
    resp = await client.post(f"{BASE}/authen/v1/oidc/refresh_access_token",
        headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
        json={"grant_type": "refresh_token", "refresh_token": refresh})
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"刷新 token 失败: {data}")
    new = data["data"]
    info = {
        "access_token": new["access_token"],
        "refresh_token": new.get("refresh_token", refresh),
        "expires_at": time.time() + new.get("expires_in", 7200),
        "name": new.get("name", token_data.get("name", "")),
    }
    _save_user_token(info)
    return info["access_token"]


# ── HTTP 重试 ────────────────────────────────────

async def _post_retry(client: httpx.AsyncClient, url: str, headers: dict, body: dict) -> httpx.Response:
    for attempt in range(MAX_RETRIES):
        resp = await client.post(url, headers=headers, json=body)
        if resp.status_code == 429:
            wait = 2 ** attempt + 1
            print(f"  429 限流，等待 {wait}s ({attempt+1}/{MAX_RETRIES})...")
            await asyncio.sleep(wait)
            continue
        return resp
    return resp


# ── URL 解析 ─────────────────────────────────────

def _parse_url(url: str) -> tuple[str, str]:
    """从飞书 URL 提取 (type, token)。type: docx/wiki/sheet/base"""
    patterns = [
        (r"/docx/([A-Za-z0-9]+)", "docx"),
        (r"/wiki/([A-Za-z0-9]+)", "wiki"),
        (r"/sheets?/([A-Za-z0-9]+)", "sheet"),
        (r"/base/([A-Za-z0-9]+)", "base"),
        (r"/doc/([A-Za-z0-9]+)", "docx"),
    ]
    for pat, typ in patterns:
        m = re.search(pat, url)
        if m:
            return typ, m.group(1)
    # 可能直接传了 token
    if re.match(r"^[A-Za-z0-9]{20,}$", url):
        return "docx", url
    raise ValueError(f"无法识别的 URL: {url}")


async def _resolve_wiki_obj_token(client: httpx.AsyncClient, node_token: str) -> str:
    """将 wiki node_token 转为 obj_token（文档 ID）。"""
    headers = await _headers(client)
    resp = await client.get(f"{BASE}/wiki/v2/spaces/get_node?token={node_token}", headers=headers)
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 wiki 节点失败: {data.get('msg')}")
    return data["data"]["node"]["obj_token"]


async def _get_doc_id(client: httpx.AsyncClient, url: str) -> str:
    """从 URL 获取文档 doc_id（自动处理 wiki→obj_token 转换）。"""
    typ, token = _parse_url(url)
    if typ == "wiki":
        return await _resolve_wiki_obj_token(client, token)
    return token


# ── 读取 ─────────────────────────────────────────

async def cmd_read(url: str, with_block_ids: bool = False) -> None:
    """读取飞书文档，输出 Markdown。"""
    import shutil
    export_dir = Path("/tmp/feishu-doc-export")
    if export_dir.exists():
        shutil.rmtree(export_dir)

    args = ["feishu-docx", "export", url, "-o", str(export_dir),
            "--app-id", CFG["app_id"], "--app-secret", CFG["app_secret"]]
    if with_block_ids:
        args.append("--with-block-ids")
    result = subprocess.run(args, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        print(f"导出失败: {result.stderr[-300:]}")
        return

    md_files = list(export_dir.glob("*.md"))
    if not md_files:
        print("未找到导出文件")
        return

    content = md_files[0].read_text(encoding="utf-8")
    print(content)


async def cmd_list_blocks(url: str) -> None:
    """列出文档所有块的 block_id、类型和内容摘要。"""
    BLOCK_NAMES = {
        1: "page", 2: "text", 3: "h1", 4: "h2", 5: "h3",
        6: "h4", 7: "h5", 8: "h6", 9: "h7", 10: "h8", 11: "h9",
        12: "bullet", 13: "ordered", 14: "code", 15: "quote",
        17: "todo", 19: "callout", 22: "divider", 27: "image",
        31: "table", 32: "table_cell",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        doc_id = await _get_doc_id(client, url)
        headers = await _headers(client)
        resp = await client.get(
            f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?page_size=200",
            headers=headers)
        data = resp.json()
        if data.get("code") != 0:
            print(f"获取失败: {data.get('msg')}")
            return

        items = data.get("data", {}).get("items", [])
        print(f"共 {len(items)} 个块\n")
        for i, item in enumerate(items):
            bt = item.get("block_type", 0)
            bid = item["block_id"]
            name = BLOCK_NAMES.get(bt, f"type_{bt}")

            # 提取内容摘要
            summary = ""
            for key in ["text", "heading1", "heading2", "heading3", "heading4",
                         "heading5", "heading6", "bullet", "ordered", "quote",
                         "todo", "code", "callout"]:
                block_data = item.get(key)
                if block_data and "elements" in block_data:
                    parts = []
                    for el in block_data["elements"]:
                        tr = el.get("text_run", {})
                        parts.append(tr.get("content", ""))
                    summary = "".join(parts)[:80]
                    break
            if bt == 22:
                summary = "---"
            if bt == 31:
                summary = f"[表格 {len(item.get('children', []))} cells]"

            done = ""
            if bt == 17:
                done_val = item.get("todo", {}).get("style", {}).get("done", False)
                done = "[x] " if done_val else "[ ] "

            print(f"  {i:>3}  {bid}  {name:<10}  {done}{summary}")


# ── 写入 ─────────────────────────────────────────

async def _write_blocks(client: httpx.AsyncClient, doc_id: str, blocks: list[dict]) -> None:
    """写入 block 列表到文档，自动拆批和处理表格。"""
    headers = await _headers(client)

    for i in range(0, len(blocks), BLOCKS_PER_BATCH):
        batch = blocks[i:i + BLOCKS_PER_BATCH]
        regular = []

        for block in batch:
            if block.get("block_type") == 31:
                if regular:
                    await _post_retry(client,
                        f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?document_revision_id=-1",
                        headers, {"children": regular})
                    regular = []
                await _write_table(client, doc_id, headers, block)
            else:
                regular.append(block)

        if regular:
            resp = await _post_retry(client,
                f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?document_revision_id=-1",
                headers, {"children": regular})
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") != 0:
                print(f"  写入失败: {result.get('msg')}")


async def _write_table(client: httpx.AsyncClient, doc_id: str, headers: dict, table_block: dict) -> None:
    """写入表格，超过 9 行自动拆分。"""
    table = table_block.get("table", {})
    prop = table.get("property", {})
    row_size = prop.get("row_size", 1)
    col_size = prop.get("column_size", 1)
    cells = table.get("cells", [])

    async def write_single(rows, cols, cell_data):
        resp = await _post_retry(client,
            f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?document_revision_id=-1",
            headers, {"children": [{"block_type": 31, "table": {"property": {"row_size": rows, "column_size": cols}}}]})
        result = resp.json()
        if result.get("code") != 0:
            return
        children = result.get("data", {}).get("children", [{}])
        cell_ids = children[0].get("children", [])
        idx = 0
        for row in cell_data:
            for cell_content in row:
                if idx >= len(cell_ids) or not cell_content:
                    idx += 1
                    continue
                await _post_retry(client,
                    f"{BASE}/docx/v1/documents/{doc_id}/blocks/{cell_ids[idx]}/children?document_revision_id=-1",
                    headers, {"children": [{"block_type": 2, "text": {"elements": [{"text_run": {"content": cell_content}}]}}]})
                idx += 1
                await asyncio.sleep(CELL_WRITE_DELAY)

    if row_size <= MAX_TABLE_ROWS:
        await write_single(row_size, col_size, cells)
    else:
        header = cells[0] if cells else []
        data_rows = cells[1:]
        per = MAX_TABLE_ROWS - 1
        for start in range(0, len(data_rows), per):
            chunk = [header] + data_rows[start:start + per]
            await write_single(len(chunk), col_size, chunk)


async def _clear_document(client: httpx.AsyncClient, doc_id: str) -> None:
    """清空文档所有内容块。"""
    headers = await _headers(client)
    resp = await client.get(f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?page_size=500", headers=headers)
    items = resp.json().get("data", {}).get("items", [])
    if not items:
        return
    total = len(items)
    await client.request("DELETE",
        f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete?document_revision_id=-1",
        headers=headers, json={"start_index": 0, "end_index": total})


def _md_to_blocks(md_content: str) -> list[dict]:
    """Markdown 转飞书 block 列表。"""
    from markdown2feishu.converter import MarkdownConverter
    return MarkdownConverter().convert(md_content)


async def cmd_create(title: str, content: str | None, file: str | None, wiki_parent: str | None) -> None:
    """创建飞书文档。"""
    md = content or ""
    if file:
        md = Path(file).read_text(encoding="utf-8")
    if not md and not title:
        print("请提供内容（-c 或 -f）")
        return

    blocks = _md_to_blocks(md) if md else []
    mode = CFG["mode"]
    wiki_parent = wiki_parent or CFG.get("default_parent_node", "")
    space_id = CFG.get("wiki_space_id", "")

    async with httpx.AsyncClient(timeout=120) as client:
        headers = await _headers(client)

        # 尝试 wiki 模式
        if (mode in ("wiki", "auto")) and wiki_parent and space_id:
            resp = await client.post(f"{BASE}/wiki/v2/spaces/{space_id}/nodes", headers=headers, json={
                "obj_type": "docx", "node_type": "origin",
                "title": title, "parent_node_token": wiki_parent,
            })
            data = resp.json()
            if data.get("code") == 0:
                node = data["data"]["node"]
                doc_id = node["obj_token"]
                url = f"https://feishu.cn/wiki/{node['node_token']}"
                if blocks:
                    print(f"写入内容 ({len(blocks)} 个块)...")
                    await _write_blocks(client, doc_id, blocks)
                print(f"已创建: {url}")
                return
            elif mode == "wiki":
                print(f"知识库写入失败: {data.get('msg')}")
                return
            else:
                print(f"知识库权限不足，降级为云文档模式")

        # drive 模式
        resp = await client.post(f"{BASE}/docx/v1/documents", headers=headers, json={"title": title})
        data = resp.json()
        if data.get("code") != 0:
            print(f"创建失败: {data.get('msg')}")
            return
        doc_id = data["data"]["document"]["document_id"]
        url = f"https://feishu.cn/docx/{doc_id}"
        if blocks:
            print(f"写入内容 ({len(blocks)} 个块)...")
            await _write_blocks(client, doc_id, blocks)
        print(f"已创建: {url}")
        if wiki_parent:
            print(f"提示: 如需移入知识库，执行 wiki-move {url} {wiki_parent}")


async def cmd_append(url: str, content: str | None, file: str | None) -> None:
    """向已有文档追加内容。"""
    md = content or ""
    if file:
        md = Path(file).read_text(encoding="utf-8")
    if not md:
        print("请提供内容（-c 或 -f）")
        return

    blocks = _md_to_blocks(md)
    async with httpx.AsyncClient(timeout=120) as client:
        doc_id = await _get_doc_id(client, url)
        print(f"追加 {len(blocks)} 个块...")
        await _write_blocks(client, doc_id, blocks)
        print("追加完成")


async def cmd_overwrite(url: str, content: str | None, file: str | None) -> None:
    """清空文档并重写。"""
    md = content or ""
    if file:
        md = Path(file).read_text(encoding="utf-8")
    if not md:
        print("请提供内容（-c 或 -f）")
        return

    blocks = _md_to_blocks(md)
    async with httpx.AsyncClient(timeout=120) as client:
        doc_id = await _get_doc_id(client, url)
        print("清空旧内容...")
        await _clear_document(client, doc_id)
        print(f"写入新内容 ({len(blocks)} 个块)...")
        await _write_blocks(client, doc_id, blocks)
        print("覆盖完成")


# ── 精确编辑 ─────────────────────────────────────

async def cmd_update_block(url: str, block_id: str, new_text: str) -> None:
    """更新单个块的文本内容。"""
    async with httpx.AsyncClient(timeout=30) as client:
        doc_id = await _get_doc_id(client, url)
        headers = await _headers(client)
        resp = await client.patch(
            f"{BASE}/docx/v1/documents/{doc_id}/blocks/{block_id}?document_revision_id=-1",
            headers=headers, json={
                "update_text_elements": {"elements": [{"text_run": {"content": new_text}}]}
            })
        data = resp.json()
        if data.get("code") == 0:
            print(f"已更新块 {block_id}")
        else:
            print(f"更新失败: {data.get('msg')}")


async def cmd_delete_block(url: str, block_id: str) -> None:
    """删除单个块。"""
    async with httpx.AsyncClient(timeout=30) as client:
        doc_id = await _get_doc_id(client, url)
        headers = await _headers(client)

        # 找到 block 在 children 中的 index
        resp = await client.get(
            f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children?page_size=500",
            headers=headers)
        items = resp.json().get("data", {}).get("items", [])
        idx = None
        for i, item in enumerate(items):
            if item["block_id"] == block_id:
                idx = i
                break
        if idx is None:
            print(f"未找到块 {block_id}")
            return

        resp = await client.request("DELETE",
            f"{BASE}/docx/v1/documents/{doc_id}/blocks/{doc_id}/children/batch_delete?document_revision_id=-1",
            headers=headers, json={"start_index": idx, "end_index": idx + 1})
        data = resp.json()
        if data.get("code") == 0:
            print(f"已删除块 {block_id}")
        else:
            print(f"删除失败: {data.get('msg')}")


# ── 知识库 ───────────────────────────────────────

async def cmd_wiki_tree(target: str) -> None:
    """显示知识库树形结构。"""
    async with httpx.AsyncClient(timeout=30) as client:
        headers = await _headers(client)

        # 判断输入是 space_id 还是 URL
        if "/" in target:
            _, node_token = _parse_url(target)
            resp = await client.get(f"{BASE}/wiki/v2/spaces/get_node?token={node_token}", headers=headers)
            node = resp.json().get("data", {}).get("node", {})
            space_id = node.get("space_id", "")
            root_token = node_token
            print(f"知识库: space_id={space_id}")
            print(f"根节点: {node.get('title', '')}\n")
        else:
            space_id = target
            root_token = None
            print(f"知识库: space_id={space_id}\n")

        async def print_tree(parent_token, indent=0):
            params = {"page_size": 50}
            if parent_token:
                params["parent_node_token"] = parent_token
            r = await client.get(f"{BASE}/wiki/v2/spaces/{space_id}/nodes",
                                 headers=headers, params=params)
            nodes = r.json().get("data", {}).get("items", [])
            for n in nodes:
                prefix = "  " * indent + ("+" if n.get("has_child") else "-")
                print(f"{prefix} {n.get('title')} [{n.get('obj_type')}]")
                if n.get("has_child"):
                    await print_tree(n["node_token"], indent + 1)

        await print_tree(root_token)


async def cmd_wiki_move(doc_url: str, parent_node: str) -> None:
    """将云文档移入知识库（需 OAuth user token）。"""
    async with httpx.AsyncClient(timeout=30) as client:
        user_token = await _get_user_token(client)
        headers = {"Authorization": f"Bearer {user_token}", "Content-Type": "application/json"}

        _, doc_token = _parse_url(doc_url)
        space_id = CFG.get("wiki_space_id", "")
        if not space_id:
            print("请在 config.yaml 中配置 wiki_space_id")
            return

        resp = await client.post(f"{BASE}/wiki/v2/spaces/{space_id}/nodes", headers=headers, json={
            "obj_type": "docx", "obj_token": doc_token,
            "parent_node_token": parent_node, "node_type": "origin",
        })
        data = resp.json()
        if data.get("code") == 0:
            node = data["data"]["node"]
            print(f"已移入知识库: https://feishu.cn/wiki/{node['node_token']}")
        else:
            print(f"移入失败: {data.get('msg')}")


async def cmd_wiki_sync(md_path: str, parent_node: str | None) -> None:
    """同步 Markdown 文件到知识库（幂等）。"""
    path = Path(md_path)
    if not path.exists():
        print(f"文件不存在: {md_path}")
        return

    # 提取标题
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^#\s+(.+)$", text, re.MULTILINE)
    title = m.group(1).strip() if m else path.stem

    # 幂等检查
    registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8")) if REGISTRY_PATH.exists() else {}
    key = path.name
    if key in registry:
        print(f"已同步过「{title}」: {registry[key]['url']}")
        return

    parent = parent_node or CFG.get("default_parent_node", "")
    if not parent:
        print("请指定 --parent 或在 config.yaml 中配置 default_parent_node")
        return

    # 创建并写入
    blocks = _md_to_blocks(text)
    async with httpx.AsyncClient(timeout=120) as client:
        headers = await _headers(client)
        space_id = CFG.get("wiki_space_id", "")

        # 检查同名节点
        if space_id:
            resp = await client.get(f"{BASE}/wiki/v2/spaces/{space_id}/nodes",
                headers=headers, params={"parent_node_token": parent, "page_size": 50})
            existing = resp.json().get("data", {}).get("items", [])
            for n in existing:
                if n.get("title") == title:
                    url = f"https://feishu.cn/wiki/{n['node_token']}"
                    print(f"已存在同名节点: {url}")
                    registry[key] = {"title": title, "url": url, "synced_at": time.strftime("%Y-%m-%d %H:%M:%S")}
                    REGISTRY_PATH.write_text(json.dumps(registry, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    return

        # 创建
        await cmd_create(title, text, None, parent)

        # TODO: 记录到 registry（create 成功后的 URL 需要传递出来）
        print(f"同步完成")


async def cmd_export_wiki(target: str, output: str, max_depth: int = 3) -> None:
    """批量导出知识库。"""
    args = ["feishu-docx", "export-wiki-space", target, "-o", output, "--max-depth", str(max_depth),
            "--app-id", CFG["app_id"], "--app-secret", CFG["app_secret"]]
    result = subprocess.run(args, capture_output=False, timeout=300)
    if result.returncode != 0:
        print("导出失败")


async def cmd_import_wechat(url: str) -> None:
    """微信文章→飞书文档。"""
    args = ["feishu-docx", "create", "--url", url,
            "--app-id", CFG["app_id"], "--app-secret", CFG["app_secret"]]
    result = subprocess.run(args, capture_output=False, timeout=60)


# ── 群消息 ───────────────────────────────────────

async def cmd_notify(title: str, content_md: str) -> None:
    """发群卡片消息。"""
    chat_id = CFG.get("notify_chat_id", "")
    if not chat_id:
        print("请配置 notify_chat_id")
        return

    card = json.dumps({
        "elements": [{"tag": "markdown", "content": content_md}],
        "header": {"template": "blue", "title": {"content": title, "tag": "plain_text"}},
    }, ensure_ascii=False)

    async with httpx.AsyncClient(timeout=15) as client:
        headers = await _headers(client)
        resp = await client.post(f"{BASE}/im/v1/messages",
            headers=headers, params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "interactive", "content": card})
        data = resp.json()
        if data.get("code") == 0:
            print("卡片已发送")
        else:
            print(f"发送失败: {data.get('msg')}")


async def cmd_send(text: str) -> None:
    """发群文本消息。"""
    chat_id = CFG.get("notify_chat_id", "")
    if not chat_id:
        print("请配置 notify_chat_id")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        headers = await _headers(client)
        resp = await client.post(f"{BASE}/im/v1/messages",
            headers=headers, params={"receive_id_type": "chat_id"},
            json={"receive_id": chat_id, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)})
        data = resp.json()
        if data.get("code") == 0:
            print("消息已发送")
        else:
            print(f"发送失败: {data.get('msg')}")


async def cmd_read_chat(count: int = 10) -> None:
    """读取群消息。"""
    import datetime
    chat_id = CFG.get("notify_chat_id", "")
    if not chat_id:
        print("请配置 notify_chat_id")
        return

    async with httpx.AsyncClient(timeout=15) as client:
        headers = await _headers(client)
        resp = await client.get(f"{BASE}/im/v1/messages", headers=headers,
            params={"container_id_type": "chat", "container_id": chat_id,
                    "page_size": count, "sort_type": "ByCreateTimeDesc"})
        data = resp.json()
        if data.get("code") != 0:
            print(f"读取失败: {data.get('msg')}")
            return
        items = data.get("data", {}).get("items", [])
        if not items:
            print("没有消息")
            return
        for msg in items:
            sender_type = msg.get("sender", {}).get("sender_type", "")
            msg_type = msg.get("msg_type", "")
            ts = msg.get("create_time", "")
            try:
                time_str = datetime.datetime.fromtimestamp(int(ts) / 1000).strftime("%m-%d %H:%M")
            except (ValueError, OSError):
                time_str = "?"
            body = msg.get("body", {})
            try:
                content = json.loads(body.get("content", "{}"))
            except (json.JSONDecodeError, TypeError):
                content = {}
            if msg_type == "text":
                text = content.get("text", "")
            elif msg_type == "interactive":
                text = "[卡片消息]"
            else:
                text = f"[{msg_type}]"
            label = "bot" if sender_type == "app" else "user"
            print(f"  {time_str}  {label}: {text[:200]}")


# ── OAuth 登录 ───────────────────────────────────

def cmd_login() -> None:
    """OAuth 登录获取 user_access_token。"""
    app_id = CFG["app_id"]
    port = 9876
    redirect_uri = f"http://localhost:{port}/callback"
    auth_url = f"{BASE}/authen/v1/authorize?app_id={app_id}&redirect_uri={urllib.parse.quote(redirect_uri)}&state=feishu_doc"

    received = {"code": None}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            code = params.get("code", [None])[0]
            if code:
                received["code"] = code
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write("授权成功！可以关闭此页面。".encode("utf-8"))
            else:
                self.send_response(400)
                self.end_headers()
        def log_message(self, *a): pass

    server = http.server.HTTPServer(("localhost", port), Handler)
    server.timeout = 120

    print(f"请在浏览器中授权:\n\n  {auth_url}\n")
    webbrowser.open(auth_url)
    print("等待回调（120 秒超时）...")

    while received["code"] is None:
        server.handle_request()
        if received["code"] is None:
            break
    server.server_close()

    if not received["code"]:
        print("未收到授权码")
        return

    async def exchange():
        async with httpx.AsyncClient(timeout=15) as client:
            app_resp = await client.post(f"{BASE}/auth/v3/app_access_token/internal", json={
                "app_id": CFG["app_id"], "app_secret": CFG["app_secret"],
            })
            app_token = app_resp.json()["app_access_token"]
            resp = await client.post(f"{BASE}/authen/v1/oidc/access_token",
                headers={"Authorization": f"Bearer {app_token}", "Content-Type": "application/json"},
                json={"grant_type": "authorization_code", "code": received["code"]})
            data = resp.json()
            if data.get("code") != 0:
                print(f"换取 token 失败: {data}")
                return
            token_data = data["data"]
            _save_user_token({
                "access_token": token_data["access_token"],
                "refresh_token": token_data.get("refresh_token", ""),
                "expires_at": time.time() + token_data.get("expires_in", 7200),
                "name": token_data.get("name", ""),
            })
            print(f"登录成功！欢迎 {token_data.get('name', '用户')}")

    asyncio.run(exchange())


# ── 测试连通性 ───────────────────────────────────

async def cmd_test() -> None:
    """测试 API 连通性。"""
    print(f"App ID: {CFG['app_id'][:12]}...")
    print(f"Mode:   {CFG['mode']}\n")

    async with httpx.AsyncClient(timeout=15) as client:
        try:
            token = await _get_tenant_token(client)
            print(f"Token:  {token[:15]}...")
        except Exception as e:
            print(f"Token 获取失败: {e}")
            return

        headers = await _headers(client)

        # 文档权限
        resp = await client.post(f"{BASE}/docx/v1/documents", headers=headers,
            json={"title": "__test__"})
        data = resp.json()
        if data.get("code") == 0:
            doc_id = data["data"]["document"]["document_id"]
            print("文档创建: OK")
            await client.delete(f"{BASE}/drive/v1/files/{doc_id}?type=docx", headers=headers)
        else:
            print(f"文档创建: FAIL ({data.get('msg')})")

        # 知识库
        space_id = CFG.get("wiki_space_id", "")
        parent = CFG.get("default_parent_node", "")
        if space_id and parent:
            resp = await client.get(f"{BASE}/wiki/v2/spaces/{space_id}/nodes",
                headers=headers, params={"parent_node_token": parent, "page_size": 5})
            if resp.json().get("code") == 0:
                nodes = resp.json().get("data", {}).get("items", [])
                print(f"知识库读取: OK ({len(nodes)} 个子节点)")
            else:
                print(f"知识库读取: FAIL ({resp.json().get('msg', '')[:60]})")
        elif space_id:
            print("知识库读取: 未配置 default_parent_node")

        # 群消息
        chat_id = CFG.get("notify_chat_id", "")
        if chat_id:
            print(f"群通知:  已配置 ({chat_id[:12]}...)")

        # User token
        ut = _load_user_token()
        if ut:
            expired = time.time() > ut.get("expires_at", 0)
            print(f"OAuth:   {'已过期' if expired else '有效'} ({ut.get('name', '')})")
        else:
            print("OAuth:   未登录")


# ── CLI 入口 ─────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return

    cmd = args[0]

    def get_flag(name: str) -> str | None:
        for i, a in enumerate(args):
            if a == name and i + 1 < len(args):
                return args[i + 1]
        return None

    def has_flag(name: str) -> bool:
        return name in args

    if cmd == "read" and len(args) >= 2:
        asyncio.run(cmd_read(args[1], with_block_ids=has_flag("--with-block-ids")))

    elif cmd == "list-blocks" and len(args) >= 2:
        asyncio.run(cmd_list_blocks(args[1]))

    elif cmd == "create" and len(args) >= 2:
        asyncio.run(cmd_create(args[1], get_flag("-c"), get_flag("-f"), get_flag("--wiki")))

    elif cmd == "append" and len(args) >= 2:
        asyncio.run(cmd_append(args[1], get_flag("-c"), get_flag("-f")))

    elif cmd == "overwrite" and len(args) >= 2:
        asyncio.run(cmd_overwrite(args[1], get_flag("-c"), get_flag("-f")))

    elif cmd == "update-block" and len(args) >= 4:
        asyncio.run(cmd_update_block(args[1], args[2], " ".join(args[3:])))

    elif cmd == "delete-block" and len(args) >= 3:
        asyncio.run(cmd_delete_block(args[1], args[2]))

    elif cmd == "wiki-tree" and len(args) >= 2:
        asyncio.run(cmd_wiki_tree(args[1]))

    elif cmd == "wiki-move" and len(args) >= 3:
        asyncio.run(cmd_wiki_move(args[1], args[2]))

    elif cmd == "wiki-sync" and len(args) >= 2:
        asyncio.run(cmd_wiki_sync(args[1], get_flag("--parent")))

    elif cmd == "export-wiki" and len(args) >= 2:
        asyncio.run(cmd_export_wiki(args[1], get_flag("-o") or "./wiki_export", int(get_flag("--max-depth") or "3")))

    elif cmd == "import-wechat" and len(args) >= 2:
        asyncio.run(cmd_import_wechat(args[1]))

    elif cmd == "notify" and len(args) >= 3:
        asyncio.run(cmd_notify(args[1], " ".join(args[2:])))

    elif cmd == "send" and len(args) >= 2:
        asyncio.run(cmd_send(" ".join(args[1:])))

    elif cmd == "read-chat":
        count = int(args[1]) if len(args) >= 2 else 10
        asyncio.run(cmd_read_chat(count))

    elif cmd == "login":
        cmd_login()

    elif cmd == "test":
        asyncio.run(cmd_test())

    else:
        print(__doc__)


if __name__ == "__main__":
    main()
