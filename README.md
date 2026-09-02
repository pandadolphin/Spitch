# Spitch

> Linux 桌面下的全局热键中文语音输入工具，默认由豆包（火山引擎）实时 ASR 驱动。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](pyproject.toml)
[![Wayland | X11](https://img.shields.io/badge/display-Wayland%20%7C%20X11-green.svg)](#系统要求)

按住 **Right Ctrl** 说话，松开自动把带标点的中文（或任意 Unicode）粘进当前应用。**不依赖 IBus / fcitx5 等输入法框架**，与你已有的拼音 / 五笔输入法和平共存。Wayland、X11 都可用。

**豆包（`provider: "doubao"`）是默认、也是推荐的中文路径。** 可选接入 xAI **Grok Streaming STT**（`provider: "grok"`）；**Grok 对中文（Mandarin）的支持尚未做 live 验证**，在语言门禁通过前文档与 UI **不会**宣称 Grok 支持中文。需要稳定中文语音输入时请继续用豆包。

```
按住 Right Ctrl ──▶ 🎙 录音 ──▶ ✍ 转写 ──▶ 自动粘到光标位置
```

---

## 特性

- **全局热键**：默认长按 `RightCtrl`，也可配置为 `RightAlt`、双修饰键组合或多个备选热键
- **说话时自动暂停媒体**（v0.7.1）：通过 `playerctl` 暂停 MPRIS 播放器，松手后恢复（可关）
- **真·实时 ASR**：默认豆包 BigModel 实时端点，自带标点 + 数字归一（ITN），中文一次性输出；可选 Grok Streaming STT（见下方语言门禁）
- **多后端**：`provider: "doubao" | "grok"`，凭据分区独立；切换后需重新 probe 验证
- **绕过 IM 框架**：转写结果走"剪贴板 + 合成 `Ctrl+Shift+V`"，在 GTK / Qt / Electron / 原生 Wayland 应用里都能粘——飞书、微信、VS Code、Chrome 地址栏、Slack 全都覆盖
- **历史 / 重粘 / 控制台**（v0.5）：daemon 保留最近 50 条转写。`spitch-console` 三 tab 窗口（历史 / 日志 / 设置），托盘菜单一键打开；`spitch-cli repaste` 可绑定到任何系统快捷键，失败/想再发一遍时一键补救
- **系统托盘**：libayatana-appindicator，三态图标（空闲 / 录音中 / 正在转写）；菜单含"打开控制台""重粘最近一次"
- **配置 UI**：GTK 对话框可选 doubao / grok；缺 PyGObject 时自动退到 CLI 提示
- **凭据安全**：配置文件 chmod 600 + 原子写；凭据指纹绑定 verified 状态，改了 key 自动失效；本地 `*.key` / `grok-voice-api.key` 已 gitignore，**切勿把 API key 提交进仓库**
- **Wayland 与 X11 双栈**：自动选 `wl-copy` / `xclip` / `xsel`

## 工作原理

1. 用户态长进程 daemon 监听 `/dev/input/event*`，等配置好的修饰键组合
2. 按下时打开麦克风，PCM 流通过 WebSocket 推给当前 `provider` 对应的 ASR（默认豆包）
3. 松开后（含可配置的 `audio.release_linger_ms` linger）进入 FINALIZING：以 `inject.final_wait_seconds` 为 **stream budget**（默认 **30 秒**）等待 server 的最终结果；inject 侧会比 controller 再多等 linger + 少量 slack，避免 final 晚到被丢
4. 把结果写进剪贴板，等用户物理松开热键后，通过 `/dev/uinput` 合成 `Ctrl+Shift+V` 触发粘贴
5. 约 0.3 秒后还原原剪贴板，避免污染你下一次手动粘贴

整条链路与输入法框架无关——**不用换输入法、不用改 IBus 设置、不用 fcitx5 插件**。

## 系统要求

- Linux + Wayland 或 X11（已在 Ubuntu 24.04 / GNOME 46 上验证）
- Python 3.10+
- 系统包：`python3-evdev` + 剪贴板工具（Wayland 装 `wl-clipboard`，X11 装 `xclip` 或 `xsel`）
- 当前用户在 `input` 组（一次性 `sudo usermod -aG input $USER` + 重登）
- `/dev/uinput` 当前 session 可写（Ubuntu 24.04 logind 自动配 ACL；其他发行版可能要 udev 规则）
- **豆包路径（默认 / 推荐中文）**：火山引擎 BigASR 的 `app_key` + `access_key`（[在控制台申请](https://www.volcengine.com/docs/6561/1354869)）
- **Grok 路径（可选）**：xAI API key；语言支持对 Mandarin **未验证**（见下）

## 快速开始

```bash
git clone https://github.com/pekinlcc/Spitch.git
cd Spitch

# Wayland 用户
sudo apt-get install -y python3-evdev wl-clipboard playerctl
# X11 用户用下面这条
# sudo apt-get install -y python3-evdev xclip playerctl

./scripts/install.sh
spitch-config        # 选择 doubao（默认）或 grok，填凭据，点 "Test connection" 验证
sudo usermod -aG input $USER     # 一次性，然后重登
spitch-daemon &      # 按住 Right Ctrl 说话，松开后自动粘贴
```

`spitch-config` 会让你选 **Provider**（`doubao` / `grok`），并 **必须** 通过 “Test connection” / probe 才会写入 `verified_*` 戳记；未验证的配置 daemon 不会当完整凭据用。

完整安装流程（含 Grok、systemd 自启动、密钥处理、回滚等）见 [`docs/INSTALL.md`](docs/INSTALL.md)。

### 可选：Grok Streaming STT

1. 在 [xAI 控制台](https://console.x.ai/) 申请 API key
2. 运行 `spitch-config`，Provider 选 `grok`，填入 `api_key` 与 endpoint（默认 `wss://api.x.ai/v1/stt`）
3. 点 **Test connection**（probe 必须成功；Grok 要求收到 `transcript.done`）
4. 重启 `spitch-daemon`

**语言门禁（硬性）：** Grok 的中文（Mandarin）能力 **未做 live 验证**，当前 **不宣称** 支持中文。产品主身份仍是中文语音输入；**中文请用豆包**。在仓库内未记录 EN/ZH live checklist 通过结果之前，语言门禁保持关闭。

**回滚到豆包：** 把 `config.json` 里 `"provider"` 改回 `"doubao"`（或 `spitch-config` 再选 doubao 并 probe），然后重启 daemon。

### 密钥与凭据安全

- API key **只**写在 `~/.config/spitch/config.json`（chmod 600，原子写）
- **永远不要**把 key 提交进 git；仓库已 gitignore `grok-voice-api.key` 与 `*.key`
- 本地开发若用 key 文件，仅作人工 seed，勿把内容贴进 commit / CI / Issue
- 日志与错误信息不应回显完整 `api_key` 或 `Authorization` 头

## 配置

配置文件路径：`~/.config/spitch/config.json`（chmod 600）。常用字段：

| 字段 | 含义 | 默认值 |
|---|---|---|
| `provider` | ASR 后端：`doubao`（默认 / 推荐中文）或 `grok`（可选） | `doubao` |
| `doubao.app_key` | 火山引擎 BigASR 的 APP ID | — |
| `doubao.access_key` | 火山引擎 BigASR 的 Access Token | — |
| `doubao.endpoint` | WebSocket 接入点 | `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel` |
| `grok.api_key` | xAI API key（仅 `provider=grok` 时需要） | — |
| `grok.endpoint` | Grok Streaming STT WebSocket（须 `wss://`） | `wss://api.x.ai/v1/stt` |
| `audio.sample_rate` | 麦克风采样率 | 16000 |
| `audio.prebuffer_ms` | 常驻麦克风的环形预缓冲长度（ms）。修复"按下后说的前半截被吃掉"——按下时回放这段缓冲。设为 0 = 关闭常驻麦克风，按下才开 | `500` |
| `audio.pause_media_on_talk` | 按住说话键时用 `playerctl` 暂停正在播放的 MPRIS 媒体，松手后恢复。需安装 `playerctl` | `true` |
| `hotkey.talk_key` | 按住说话的修饰键。`RightAlt` / `RightCtrl` 可单用；双修饰键用 `+`，多个备选用逗号 | `RightCtrl` |
| `inject.paste_keystroke` | 粘贴用的合成快捷键 | `Ctrl+Shift+V` |
| `inject.restore_clipboard_delay_ms` | 粘贴后等多久才把剪贴板还原（ms） | `800` |
| `inject.final_wait_seconds` | 松手进入 FINALIZING 后，等 server final 的 **stream budget**（秒）。controller 用 `max(该值, 5)`；inject 队列等待会略长（+ linger + slack） | `30.0` |
| `history.capacity` | 最近转写历史保留条数 | `50` |

修改后保存即可：运行中的 daemon 会 `reload_config`（录音中会等松开再切）。控制台仍保留 **重启 daemon** 给卡死的进程用。

## 控制台 / 历史 / 重粘

v0.5 起 daemon 维护最近 50 条转写历史，提供三种使用方式：

- **托盘菜单**：右键托盘图标 → "打开控制台"。三 tab 窗口：
  - 历史：复制 / 重粘 / 删除任何一条
  - 日志：实时 tail `~/.local/state/spitch/daemon.log`
  - 设置：常用配置项的图形界面（不含凭据，凭据仍走 spitch-config）；保存后热加载
- **`spitch-cli`**：命令行同样能管历史
  ```bash
  spitch-cli list           # 查看历史
  spitch-cli repaste        # 重粘最近一次
  spitch-cli repaste --index 3  # 重粘第 3 条
  spitch-cli clear          # 清空
  spitch-cli reload         # 让运行中的 daemon 重读 config.json
  ```
- **绑定到系统快捷键**：把 `spitch-cli repaste` 绑到 GNOME Settings → 键盘 → 自定义快捷键（推荐 `Super+Z`）。任何时候上次粘漏了 / 想再发一遍，一个键补救。

历史持久化在 `~/.local/state/spitch/history.jsonl`（chmod 600，跨 daemon 重启保留）。

## 常见问题

**热键按下没反应？**
看 `~/.local/state/spitch/daemon.log`。最常见的原因是用户没在 `input` 组：`id | grep input` 验证；没有的话 `sudo usermod -aG input $USER` 后重登。

**转写迟钝、漏字、或 Grok 完全没字？**
先把麦克风拿近再试，不要先换模型。远场/小声时 PCM 能量很低，Grok 的 VAD 会当静音丢掉（表现为「不灵敏」、日志几乎没有 `partial:`、或 `inject: empty transcript`）。2026-08-30 实测：同一套 Grok STT，麦离远时差，贴近所实时且准。豆包同样吃音量，但远场时 Grok 更容易整段空白。

**粘贴失败？**
托盘通知会写明真实原因（缺 wl-clipboard / `/dev/uinput` 不可写 / 键名错误）。`getfacl /dev/uinput | grep $USER` 检查 ACL。

**剪贴板被乱填？**
Spitch 会在粘贴前保存原剪贴板，约 0.3 秒后还原。如果你的目标应用消费粘贴较慢，原剪贴板可能在被消费前覆盖回去；增大 `src/spitch/inject/text_injector.py` 里的 sleep。

**怎么换热键？**
`spitch-config` 的 *Push-to-talk key* 字段，或直接编辑 `config.json` 的 `hotkey.talk_key`。支持修饰键双键组合（`Ctrl/Alt/Shift/Super` 任选两个），以及单侧单键 `RightAlt` / `RightCtrl`。多个热键用逗号或 `or`：`RightAlt, RightCtrl` 表示按住其中任意一个即可。左 Alt / 左 Ctrl 仍走系统快捷键。不支持字母键。保存后运行中的 daemon 会自动热加载。

**第三键取消是什么意思？**
配置双修饰键组合时，如果按住组合后再按字母（例如 `Ctrl+Alt+T`），录音自动作废、系统快捷键正常生效。

**飞书 / 微信里粘出来是空的？**
极少数 Electron 应用对剪贴板 MIME 类型敏感。先确认 `wl-paste` 在那个应用聚焦时能拿到 Spitch 写的文本；不行的话开 Issue 贴上桌面环境信息。

**Grok 能用中文吗？**
**未验证。** 在 live Mandarin checklist 通过并写入发布说明之前，请把 Grok 当作可选后端，**不要**当作中文方案。中文请用默认的豆包。

**怎么从 Grok 退回豆包？**
`spitch-config` 选 `doubao` 并 Test connection，或编辑 `config.json` 设 `"provider": "doubao"` 后重启 daemon。

## 开发

```bash
# 单元测试（stdlib unittest，零额外依赖）
PYTHONPATH=src python3 -m unittest discover -s tests -v

# 端到端烟雾测（mock 豆包服务器 + 可选真实麦克风）
tests/e2e_smoke.sh
```

测试覆盖：
- 二进制帧编解码 (test_doubao_protocol.py)
- 配置读写 + verified 指纹 (test_config.py)
- WebSocket 流式协议 (test_doubao_client_mock.py)
- Grok Streaming STT mock (test_grok_stt_client_mock.py)
- 推到说控制器状态机 (test_voice_controller.py)
- 多后端 probe / UI (test_ui_probe.py)

## License

MIT — 见 [LICENSE](LICENSE)。

---

## English

**Spitch** is a global-hotkey **Chinese voice input** tool for Linux desktops. **Doubao (Volcano Engine) realtime ASR is the default and recommended Chinese path.** Hold **Right Ctrl** to talk, release to commit punctuated text into the focused app via clipboard + synthetic `Ctrl+Shift+V` — bypassing the input-method framework entirely. The talk key is configurable. Works on Wayland and X11 alike, in any GTK / Qt / Electron / native-Wayland app, and coexists with whatever IBus / fcitx5 setup you already have.

**Optional:** `provider: "grok"` enables xAI Grok Streaming STT. **Mandarin support for Grok is unvalidated** — do not treat Grok as a Chinese backend until a live checklist passes and release notes say so. Language gate remains closed until then.

After release, the stream budget for the final transcript is `inject.final_wait_seconds` (**default 30s**), not a fixed 5s window.

### Quick start

```bash
git clone https://github.com/pekinlcc/Spitch.git
cd Spitch
sudo apt-get install -y python3-evdev wl-clipboard   # or xclip on X11
./scripts/install.sh
spitch-config                                        # choose doubao or grok, paste credentials, Test connection
sudo usermod -aG input $USER                         # one-time, then relogin
spitch-daemon &
```

Secrets live only in `~/.config/spitch/config.json` (mode 600). Never commit API keys; `*.key` / `grok-voice-api.key` are gitignored. **Rollback:** set `"provider": "doubao"` and restart.

### Status

See [`docs/INSTALL.md`](docs/INSTALL.md) for the full English setup guide (multi-provider) and [`CHANGELOG.md`](CHANGELOG.md) for release history.

### License

MIT.
