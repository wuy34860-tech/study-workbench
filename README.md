# 学习工作台（Study Workbench）

一个"陪你学完整个方向"的学习助手，可**单机自用**，也可**部署成面向公众的网站**。输入你想学的方向，AI 帮你生成分阶段学习路线、推荐课程与教材、过关标准、动手项目和时间节奏；另有问 AI、练习批改、拓展、找资源、搜索、笔记等能力。

## 功能

- **🗺️ 学习路线**：输入方向/关键词 → AI 生成结构化的中文 Markdown 路线（方向概述 / 前置知识 / 分阶段路线 / 推荐资源 / 动手项目 / 时间与节奏建议）
- **💬 问 AI**：费曼式讲概念、查报错、总结搜索结果
- **✏️ 练习 + 批改**：自动出题，AI 逐题批改
- **🧭 拓展 / 📚 找资源**：学完延展进阶方向、相关领域、真实应用与论文
- **🔍 多源搜索**：全网(DuckDuckGo) + B 站 + 本地笔记 + LLM Wiki
- **📝 笔记/成就/任务**：注册后独立知识库
- **多 Provider**：Anthropic Claude / OpenAI 兼容（Qwen · DeepSeek · 硅基流动 · GPT · Kimi · GLM）/ Gemini / 内置 demo

## 运行

Windows 单机：双击 `start.bat`（服务以无窗口后台运行），或命令行：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py            # 默认 http://127.0.0.1:5000
```

### 命令行 / 环境变量

| 参数 | 说明 |
|---|---|
| `python app.py --public` | 监听 `0.0.0.0`（公网服务器/容器），不自动开浏览器 |
| `python app.py --host 0.0.0.0 --port 8080` | 自定义地址与端口 |
| 环境变量 `PORT` | 端口，默认 5000 |
| 环境变量 `STUDY_NO_BROWSER=1` | 不自动打开浏览器 |

## AI 接入（三层取值，优先级从高到低）

1. **登录用户自己的 Key**：每用户在「记录 → 设置」填 `provider / model / base_url / api_key`
2. **服务器统一 Key（环境变量）**：公开站点为所有未填 Key 的访客服务
3. **内置 demo（兜底）**：都没有 Key 时自动返回**标注清晰的内置示例**，保证页面链路可用

环境变量：`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `DASHSCOPE_API_KEY` / `DEEPSEEK_API_KEY` / `SILICONFLOW_API_KEY` / `GEMINI_API_KEY`（详见 `.env.example`）。

> 常见踩坑（保存时会自动纠正并提示）：DeepSeek 的 API 地址是 `https://api.deepseek.com`（**不是** `platform.deepseek.com` 控制台）；公开 API 模型名是 `deepseek-chat` / `deepseek-reasoner`。

## 部署成公开网站（推荐）

```bash
# 1. 服务器上安装依赖
python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt   # Linux: .venv/bin/python ...

# 2. 配好服务器级 AI Key（访客不需要各自填 Key）
export DEEPSEEK_API_KEY=sk-xxxx
#    （或用 DASHSCOPE_API_KEY / OPENAI_API_KEY / ANTHROPIC_API_KEY 之一）

# 3. 以公网模式启动
.venv/bin/python app.py --public --port 8000
```

然后：

- **反向代理 + 域名（推荐生产）**：Nginx/Caddy 转发到 `127.0.0.1:8000`，绑定你的域名并配 HTTPS；
- **局域网内测**：直接访问 `http://服务器IP:8000`；
- **WorkBuddy「发布为应用」**：在 WorkBuddy 中把项目目录作为应用发布，得到临时在线链接，适合小范围体验/演示。

### 公开站点的行为（本次已内置）

- **核心功能免登录**：学习路线、问 AI、练习、拓展、找资源、搜索，游客可直接用；
- **保存/笔记/成就/记录需登录**：注册开放（`/register`），每人数据独立隔离；
- **限流**：匿名按 IP 每小时最多 40 次 AI 请求，登录用户每小时 800 次（防滥用/控成本）；
- **隐私**：游客的搜索不读本地笔记与 LLM Wiki（那是登录用户私有的）；
- **无 Key 时**：自动降级为内置 demo，页面照常演示，不报错。

> 上线提示：若对外开放，请务必配服务器级 Key + 域名 HTTPS；首次使用前在「记录 → 设置」里点一次「测试连接」确认模型可通。

## 常见问题

- **AI 生成失败/密钥无效**：多半是四件套填错。用「一键填入预设」或直接点「测试连接」看真实报错；DeepSeek 用户注意 base_url 与模型名（见上）。
- **搜索全网没结果**：走 DuckDuckGo，大陆网络可能限流；可稍后重试，本地/B 站搜索不受影响。
- **模型名无效**：点「📋 一键填入预设」套用各家推荐模型；不要臆造模型名。
- **端口被占用**：`--port` 换一个，或 `stop.bat` 清掉 5000。
- **B 站搜不到**：B 站 WBI 签名表会周期性失效，失效时接口返回空并给出提示，属正常。
- **start.bat 点了没反应**：Windows cmd 默认 GBK，请确保 start.bat 内运行命令为英文（本项目已处理）；服务为无窗口后台运行，启动后看浏览器是否自动打开 `http://127.0.0.1:5000`。
