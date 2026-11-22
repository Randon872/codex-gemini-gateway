# 准备 Gemini 本机后端

本项目是 Codex 与 Gemini 后端之间的路由层，不包含 Google 登录实现。

1. 从 [AIStudio2API 官方 Releases](https://github.com/Mag1cFall/AIStudio2API/releases/tag/v0.3.13) 下载适合操作系统和 CPU 的发行包，核对发行摘要。原部署使用 v0.3.13，具体安装方式以对应版本说明为准。
2. 使用该程序的账户管理界面登录自己的 Google 账号。macOS 命令行发行版可按上游说明运行 `./aistudio2api setup --login`。Google 登录、双重验证和授权由本人在可信页面完成，不向本项目或维护者提供密码、Cookie、验证码。
3. 在上游 `.env` 中设置本机监听 `LISTEN_ADDR=127.0.0.1:2048`、独立的 `PROXY_API_KEY`，并保持 `REQUEST_BODY_LOG=false`。需要随启动运行生成服务时使用 `AUTO_START=true`。不要把真实 `.env` 放进此仓库。
4. 启动后端，确认其管理页服务状态正常，并在它的实时模型目录中核对 `gemini-4-neon` 和 `gemini-4-hydrogen`。个人会员身份本身不能证明这些模型对你的账号可用。
5. 先在后端的 API 试用页分别完成一次普通请求；再回到本项目运行配置生成器。

后端的账户会话、登录续期、浏览器运行时安装和自启由 AIStudio2API 管理。本项目生成的 LaunchAgent 只启动路由网关，不启动后端。上游版本变化后应重新验证。

参考：[固定版本配置示例](https://github.com/Mag1cFall/AIStudio2API/blob/v0.3.13/.env.example)、[固定版本说明](https://github.com/Mag1cFall/AIStudio2API/blob/v0.3.13/README.md)。
