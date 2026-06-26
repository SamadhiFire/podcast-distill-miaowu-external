# 字幕提取工具说明

本仓库最终保留的方案是 Python CLI：`extract_subtitles.py`。

## 为什么选这些工具

- `yt-dlp`：覆盖 YouTube 和 Bilibili，适合拿原生字幕，也能在无字幕时下载音频给 ASR。
- `youtube-transcript-api`：YouTube 备用通道，本地测试用户给的 YouTube 视频可拿到英文人工字幕。
- Bilibili player subtitle API：比直接让 `yt-dlp` 拉格式元数据更稳；本地测试中 `yt-dlp` 遇到 412，但公开 player API 可正常返回字幕字段。
- 小宇宙：没有找到成熟可直接依赖的 GitHub 工具；脚本会先解析页面里的 `transcriptMediaId`，有登录态时请求官方字幕接口，拿不到时再走 whisper.cpp 转写。

## 本地使用

```powershell
.\.venv\Scripts\python.exe extract_subtitles.py "https://www.youtube.com/watch?v=CFqjjKp9Y-Q" --output subtitles
.\.venv\Scripts\python.exe extract_subtitles.py --batch config\urls.txt --output subtitles
```

如果没有本地 Python，可以用 Codex 运行时创建 `.venv` 后安装：

```powershell
& "C:\Users\AS\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 小宇宙官方字幕

小宇宙官方字幕接口需要登录态。不要把 token 写进文件，建议只放环境变量：

```powershell
$env:XIAOYUZHOU_ACCESS_TOKEN="你的 x-jike-access-token"
.\.venv\Scripts\python.exe extract_subtitles.py "https://www.xiaoyuzhoufm.com/episode/..." --output subtitles
```

GitHub Actions 里放到 `Settings -> Secrets and variables -> Actions`：

- `XIAOYUZHOU_ACCESS_TOKEN`
- `BILIBILI_COOKIE`（可选，B 站遇到登录字幕或 412 时用）
- `YTDLP_COOKIES_B64`（可选，Netscape cookies.txt 的 base64，用于 YouTube/B 站风控）

已经暴露过的 token 建议退出小宇宙/即刻重新登录或刷新登录态。
