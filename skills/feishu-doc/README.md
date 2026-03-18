# feishu-doc

飞书文档 AI Skill — 读写飞书云文档和知识库的统一工具。

融合了 [feishu-docx](https://github.com/leemysw/feishu-docx)（读取/导出）、块级 API（精确编辑）和生产级写入管道（表格拆分、限流重试、幂等保护）。

## 快速开始

### 1. 安装依赖

需要 **Python >= 3.11**（推荐 `uv run --python 3.12`）。

```bash
pip install -r requirements.txt
```

### 2. 配置凭证

**方式 A：环境变量（推荐）**
```bash
export FEISHU_APP_ID="cli_xxx"
export FEISHU_APP_SECRET="your_secret"
```

**方式 B：配置文件**

编辑 `config.yaml`，填入 `app_id` 和 `app_secret`。

### 3. 验证

```bash
python feishu_doc.py test
```

### 4. 飞书应用权限

在 [飞书开放平台](https://open.feishu.cn) 为应用开通：

| 权限 | 用途 | 必需 |
|------|------|------|
| `docx:document` | 文档读写 | 是 |
| `docx:document:readonly` | 文档只读 | 是 |
| `wiki:wiki:readonly` | 知识库读取 | 推荐 |
| `wiki:wiki` | 知识库写入 | 可选 |
| `im:message` | 群消息读写 | 可选 |
| `drive:drive` | 云盘管理 | 可选 |

## 命令一览

### 读取
```bash
python feishu_doc.py read <URL>                    # 文档→Markdown
python feishu_doc.py read <URL> --with-block-ids   # 带块 ID（编辑前用）
python feishu_doc.py list-blocks <URL>             # 列出所有块
python feishu_doc.py wiki-tree <URL>               # 知识库结构
python feishu_doc.py export-wiki <URL> -o ./out    # 批量导出
```

### 写入
```bash
python feishu_doc.py create "标题" -c "Markdown"   # 从内容创建
python feishu_doc.py create "标题" -f ./file.md    # 从文件创建
python feishu_doc.py create "标题" --wiki <node>   # 创建到知识库
python feishu_doc.py append <URL> -c "追加内容"     # 追加
python feishu_doc.py overwrite <URL> -f ./new.md   # 清空重写
python feishu_doc.py import-wechat <微信URL>       # 微信文章→飞书
```

### 精确编辑
```bash
python feishu_doc.py update-block <URL> <block_id> "新内容"
python feishu_doc.py delete-block <URL> <block_id>
```

### 知识库
```bash
python feishu_doc.py wiki-spaces                                   # 列出可访问的知识库
python feishu_doc.py wiki-sync <file.md> --parent <node>           # 同步（幂等）
python feishu_doc.py wiki-move <URL> <parent_node>                 # 移入知识库
python feishu_doc.py wiki-move <URL> <parent_node> --title "标题"  # 移入并设标题
```

### 权限管理
```bash
python feishu_doc.py permission <URL> editable   # 组织内可编辑
python feishu_doc.py permission <URL> viewable   # 组织内可查看
python feishu_doc.py permission <URL> public     # 互联网可查看
python feishu_doc.py permission <URL> closed     # 关闭链接分享
```

### 群消息
```bash
python feishu_doc.py notify "标题" "Markdown内容"
python feishu_doc.py send "文本消息"
python feishu_doc.py read-chat 10
```

### 管理
```bash
python feishu_doc.py test     # 测试连通性
python feishu_doc.py login    # OAuth 登录（可选，wiki-move 的 fallback）
```

## 作为 AI Skill 使用

将 `skills/feishu-doc/` 目录放到你的项目中，AI 代理会通过 `SKILL.md` 自动识别何时调用。

支持 Claude Code、Cursor、OpenCode 等 AI 编码工具。

## 写入模式

| 模式 | 说明 |
|------|------|
| `auto` | 先尝试 wiki，权限不足自动降级为 drive |
| `wiki` | 直接创建知识库节点（需 Bot 为空间成员） |
| `drive` | 创建云文档（权限要求最低） |

## 已知限制

- 知识库写入需要 Bot 被添加为空间「可编辑」成员
- 公开知识库无法通过 API 添加 Bot，需人工操作
- `wiki-move` 优先使用 tenant token，失败时 fallback 到 OAuth
- 表格超过 9 行会自动拆分（飞书 API 限制）
- `read` 命令依赖 `feishu-docx` 的全局凭证配置
