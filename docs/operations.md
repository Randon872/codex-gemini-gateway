# 运维与排查

## 用户登录后自启（macOS）

先在前台完成验证，再按 Ctrl+C 停止前台网关。保持仓库位于固定路径；生成后移动文件夹会使绝对路径失效。

```sh
mkdir -p "$HOME/Library/LaunchAgents"
cp runtime/local.codex-gemini-gateway.plist "$HOME/Library/LaunchAgents/"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.codex-gemini-gateway.plist"
launchctl print "gui/$(id -u)/local.codex-gemini-gateway"
```

已有同名服务时先查现状，不要重复 bootstrap。`RunAtLoad` 和 `KeepAlive` 负责登录后启动及异常重启；这不是无人登录时的系统服务。AIStudio2API 后端需要单独保持运行。

停止与重新启动：

```sh
launchctl bootout "gui/$(id -u)/local.codex-gemini-gateway"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/local.codex-gemini-gateway.plist"
```

不想下次登录再启动时，将对应 plist 移到其它目录保留。停止网关期间，使用此提供商的新模型请求会失败；这不会改变系统网络。

## 日志和错误

`runtime/routes.jsonl` 仅记录随机请求 ID、模型、路由、状态和时间，单文件最多约 5 MB，保留两份轮转。服务标准输出和错误日志另在 runtime 中；后端日志是独立的，应分别检查。

| 现象 | 检查方向 |
| --- | --- |
| 端口占用 | `lsof -nP -iTCP:2052 -sTCP:LISTEN`；不要杀死不属于自己的进程 |
| 401 | 网关凭据导出命令、ChatGPT 登录、后端 API Key；不输出真实 token 排查 |
| 未知模型 400 | 本发行版只支持两个明确的 Gemini ID 和本地 OpenAI 白名单 |
| 502 或中途断开 | 后端是否运行、Google 会话、网络、超时、服务错误日志 |
| 有 200 但一直等待 | 等待完整流结束；高推理可能较慢，不能只凭状态码判断成功 |
| 菜单缺失 | 顶层 TOML 位置、模型目录绝对路径、新聊天、实际生效提供商和应用版本 |
| 推理签名错误 | 确认使用此网关新建的聊天；旧对话的未标记异厂商推理记录未适配 |

## 更新与还原

模型清单在 `runtime/models.json`；OpenAI 路由白名单来自 `runtime/openai-fallback-models.json`。刷新时两者要一致，保留两个 Gemini 的显式路由。不要直接在仍在使用的 runtime 上重新运行生成器。

先备份私有 runtime 和 Codex 配置，在另一目录准备新配置；验证成功后再停服务、替换和重开。更新代码前可保留当前 tag，出问题回到原 tag。不要公开包含凭据的 runtime 压缩包。

`sh runtime/restore.sh` 可离线恢复生成器保存的完整 Codex 配置并停止用户后台服务；先确认备份时间，以及是否需要另存后续配置更改。它不删除后端，也不删除私有数据。按需要保留恢复材料，最终删除前自行确认。
