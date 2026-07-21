# 🎬 影片解释器

> 基于多模态模型的本地视频分析工具：生成概览、音频转写、分段拆解与仿拍参考。

Contact: **Jacksun** · [qinji@jack-sun.com](mailto:qinji@jack-sun.com)

![video-explainer project visual](docs/assets/video-explainer-hero.png)

基于 qwen3.7-plus 多模态大模型的视频分析工具。

## 功能

| 工具 | 用途 |
|------|------|
| `scripts/understand_video.py` | 快速分析单个视频片段 |
| `scripts/analyze_video.py` | 全流程分析（概览+音频转写+逐段拆解+仿拍参考） |

## 快速开始

```bash
# 快速看一段视频
python3 scripts/understand_video.py 视频文件.mp4

# 全流程深度分析
python3 scripts/analyze_video.py 视频文件.mp4

# 自定义分段时长（默认15秒）
python3 scripts/analyze_video.py 视频文件.mp4 --segment-duration 10

# 指定分析模式
python3 scripts/analyze_video.py 视频文件.mp4 --mode reference

# 跳过确认
python3 scripts/analyze_video.py 视频文件.mp4 --yes
```

## 分析模式

| 模式 | 说明 |
|------|------|
| `general` | 通用画面理解 |
| `shot` | 逐镜头拆解 |
| `technical` | 技术细节（构图、调色、灯光） |
| `reference` | 仿拍参考（默认，最详细） |

## 流程

1. 上传视频到阿里云 OSS
2. 调用 qwen3.7-plus 分析全片概览
3. 按时间分段，逐段分析画面
4. 提取音频 → Paraformer 转文字
5. 生成完整分析报告

## 费用

基于 qwen3.7-plus 实际定价（输入2元/百万Token，输出8元/百万Token）：
- 5秒片段：约 ¥0.02
- 1分钟全片：约 ¥0.15-0.30
- 1.5分钟全片（含分段）：约 ¥0.50-0.80

每次运行会先显示预估费用，确认后执行。

## 配置

复用 `aliyun-isi` 技能的阿里云凭证（通过 `.env` 符号链接）。

需要以下环境变量：
- `ALIYUN_ACCESS_KEY_ID`
- `ALIYUN_ACCESS_KEY_SECRET`
- `ALIYUN_OSS_BUCKET`
- `ALIYUN_OSS_ENDPOINT`
- `DASHSCOPE_API_KEY` 或 `BAILIAN_API_KEY`

## 依赖

```bash
pip3 install oss2 python-dotenv requests
```

需要 ffmpeg (音频提取)。
