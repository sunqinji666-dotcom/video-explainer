#!/usr/bin/env python3
"""
影片解释器 - 全流程影片分析

工作流程：
1. 上传视频到阿里云OSS
2. 调用 qwen3.7-plus 全片概览分析
3. 按时间分段，逐段分析画面
4. 提取音频 → Paraformer 转文字
5. 生成完整分析报告（含仿拍参考）

用法：
  python3 analyze_video.py /path/to/video.mp4
  python3 analyze_video.py /path/to/video.mp4 --segment-duration 10
  python3 analyze_video.py /path/to/video.mp4 --mode general
  python3 analyze_video.py /path/to/video.mp4 --output report.md
  python3 analyze_video.py /path/to/video.mp4 --yes
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv


# ── Config ──────────────────────────────────────────────
QWEN_MODEL = 'qwen3.7-plus'
QWEN_INPUT_PRICE = 0.002    # 元/千token
QWEN_OUTPUT_PRICE = 0.008   # 元/千token

PROJECT_DIR = Path(__file__).parent.parent


# ── Logging ────────────────────────────────────────────

def log(msg):
    print(f'  🎬 {msg}', flush=True)


def log_step(step, msg):
    print(f'\n  [{step}] {msg}', flush=True)


# ── Credentials ────────────────────────────────────────

def load_credentials():
    paths = [
        PROJECT_DIR / '.env',
        Path.home() / '.codex' / 'skills' / 'aliyun-isi' / '.env',
    ]
    for p in paths:
        if p.exists():
            load_dotenv(p)
            return

    print('错误: 未找到 .env 文件', file=sys.stderr)
    print(f'尝试路径: {paths}', file=sys.stderr)
    sys.exit(1)


def check_env():
    required = [
        'ALIYUN_ACCESS_KEY_ID', 'ALIYUN_ACCESS_KEY_SECRET',
        'ALIYUN_OSS_BUCKET', 'ALIYUN_OSS_ENDPOINT',
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        print(f'错误: 缺少必要环境变量: {missing}', file=sys.stderr)
        sys.exit(1)

    # DASHSCOPE key can be either name
    if not os.environ.get('DASHSCOPE_API_KEY') and not os.environ.get('BAILIAN_API_KEY'):
        print('错误: 缺少 DASHSCOPE_API_KEY 或 BAILIAN_API_KEY', file=sys.stderr)
        sys.exit(1)


def get_api_key():
    return os.environ.get('DASHSCOPE_API_KEY') or os.environ.get('BAILIAN_API_KEY')


# ── OSS ────────────────────────────────────────────────

def get_oss_bucket():
    import oss2
    auth = oss2.Auth(os.environ['ALIYUN_ACCESS_KEY_ID'], os.environ['ALIYUN_ACCESS_KEY_SECRET'])
    return oss2.Bucket(auth, os.environ['ALIYUN_OSS_ENDPOINT'], os.environ['ALIYUN_OSS_BUCKET'])


def upload_to_oss(local_path):
    """Upload file to OSS and return (public_url, oss_key)"""
    bucket = get_oss_bucket()
    file_id = uuid.uuid4().hex[:8]
    ext = Path(local_path).suffix
    oss_key = f'video-analyzer/{file_id}{ext}'
    bucket.put_object_from_file(oss_key, local_path)
    url = bucket.sign_url('GET', oss_key, 7200)
    return url, oss_key


def delete_from_oss(oss_key):
    try:
        get_oss_bucket().delete_object(oss_key)
    except:
        pass


# ── Qwen API ───────────────────────────────────────────

def call_qwen(video_url, prompt, max_tokens=4000, timeout=180):
    import requests

    full_prompt = f'{prompt}\n\n用清晰的中文回答。不要使用markdown表格。'

    payload = {
        'model': QWEN_MODEL,
        'messages': [{
            'role': 'user',
            'content': [
                {'type': 'video_url', 'video_url': {'url': video_url}},
                {'type': 'text', 'text': full_prompt}
            ]
        }],
        'max_tokens': max_tokens,
    }

    headers = {
        'Authorization': f'Bearer {get_api_key()}',
        'Content-Type': 'application/json',
    }

    api_url = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'

    resp = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
    if resp.status_code != 200:
        raise Exception(f'API错误 {resp.status_code}: {resp.text[:200]}')

    result = resp.json()
    content = result['choices'][0]['message']['content']
    usage = result.get('usage', {})

    return content, usage


# ── Audio ──────────────────────────────────────────────

def extract_audio(video_path, audio_path):
    subprocess.run([
        'ffmpeg', '-i', video_path, '-vn',
        '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1',
        '-y', audio_path
    ], capture_output=True, check=True)


def transcribe_audio(audio_path):
    """Transcribe using aliyun-isi Paraformer"""
    paraformer = Path.home() / '.codex' / 'skills' / 'aliyun-isi' / 'scripts' / 'transcribe_paraformer_local.py'
    txt_out = audio_path + '.txt'

    if not paraformer.exists():
        log(f'警告: Paraformer脚本不存在 ({paraformer})，跳过音频转写')
        return None

    try:
        result = subprocess.run(
            ['python3', str(paraformer), audio_path, '--txt-out', txt_out],
            capture_output=True, text=True, timeout=120
        )
        if os.path.exists(txt_out):
            with open(txt_out, 'r', encoding='utf-8') as f:
                text = f.read().strip()
            try: os.remove(txt_out)
            except: pass
            return text
        else:
            log(f'Paraformer失败: {result.stderr[-200:]}')
            return None
    except Exception as e:
        log(f'转写出错: {e}')
        return None


# ── Video Info ─────────────────────────────────────────

def get_video_info(video_path):
    result = subprocess.run(
        ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', '-show_streams', video_path],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)
    fmt = data.get('format', {})
    duration = float(fmt.get('duration', 0))
    vs = next((s for s in data.get('streams', []) if s['codec_type'] == 'video'), {})
    audio = next((s for s in data.get('streams', []) if s['codec_type'] == 'audio'), None)
    return {
        'duration': duration,
        'width': vs.get('width', 0),
        'height': vs.get('height', 0),
        'has_audio': audio is not None,
        'size': os.path.getsize(video_path),
    }


# ── Segmentation ───────────────────────────────────────

def calc_segments(duration, seg_duration=15):
    segments = []
    start = 0
    idx = 1
    while start < duration - 1:
        end = min(start + seg_duration, duration)
        segments.append({'index': idx, 'start': round(start), 'end': round(end)})
        start = end
        idx += 1
    return segments


def cut_segment(video_path, segment, output_path):
    subprocess.run([
        'ffmpeg', '-i', video_path,
        '-ss', str(segment['start']), '-t', str(segment['end'] - segment['start']),
        '-c', 'copy', '-y', output_path
    ], capture_output=True)


# ── Prompts ────────────────────────────────────────────

OVERVIEW_PROMPT = '''你是一个专业的影视分析专家和导演。请分析这个完整的影片，输出以下内容：

1. 影片概览：类型、风格、主题、目标受众
2. 叙事结构：开篇、发展、高潮、结尾
3. 情绪曲线：整片的情绪变化节奏
4. 视觉风格：整体构图特点、色彩调性、光线风格
5. 剪辑手法：节奏、转场、景别变化规律
6. 声音设计：音乐风格、音效运用
7. 总体评价：优缺点、可借鉴的拍摄手法'''

SEGMENT_PROMPT = '''这是影片的一个片段（第{idx}段，{start}s - {end}s）。以仿拍为目的分析：

### 1. 镜头拆解
每个镜头列出：画面内容 | 机位 | 镜头焦段推荐 | 光圈 | 灯光布置 | 调度方法

### 2. 灯光与调色
- 光影特点
- 色彩倾向
- 后期调色方向

### 3. 给拍摄者的建议
- 设备推荐
- 时间/参数建议
- 这个片段中最值得学习的拍摄技巧'''

STYLE_PROMPT = '''分析这个影片的视觉风格：
1. 色彩调性
2. 光线风格
3. 构图偏好
4. 镜头语言特点
5. 后期风格
6. 可效仿的拍摄技巧'''


# ── Report Generation ──────────────────────────────────

def generate_report(data):
    lines = []
    lines.append('# 🎬 影片分析报告\n')
    lines.append(f'**影片**: {data["video_name"]}  ')
    lines.append(f'**时长**: {int(data["duration"]//60)}:{int(data["duration"]%60):02d}  ')
    lines.append(f'**分析时间**: {datetime.now().strftime("%Y-%m-%d %H:%M")}  ')
    lines.append(f'**Token消耗**: {data["total_tokens"]}  ')
    lines.append(f'**预估费用**: ¥{data["estimated_cost"]}\n')
    lines.append('---\n')

    lines.append('## 一、影片总览\n')
    lines.append(data.get('overview', '（无）') + '\n')

    if data.get('style_analysis'):
        lines.append('\n## 二、视觉风格分析\n')
        lines.append(data['style_analysis'] + '\n')

    n = '三' if data.get('style_analysis') else '二'
    lines.append(f'\n## {n}、音频转写\n')
    if data.get('transcript'):
        lines.append(data['transcript'] + '\n')
    else:
        lines.append('（无音频或转写失败）\n')

    n = '四' if data.get('style_analysis') else '三'
    lines.append(f'\n## {n}、逐段分析\n')
    for seg in data.get('segments', []):
        lines.append(f'\n### 片段 {seg["index"]}: {seg["start"]}s - {seg["end"]}s\n')
        lines.append(seg['analysis'] + '\n')

    return '\n'.join(lines)


# ── Cost Estimate ──────────────────────────────────────

def estimate_cost(duration, seg_duration=15):
    seg_count = max(1, int(duration / seg_duration))
    total_input = min(int(duration * 600), 30000) + seg_count * 5000
    total_output = 6000 + seg_count * 2500
    cost = (total_input / 1000 * QWEN_INPUT_PRICE * 0.66 +
            total_output / 1000 * QWEN_OUTPUT_PRICE * 0.34)
    return round(cost, 4), seg_count


# ── Main ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='🎬 影片解释器 - 全流程影片分析',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  %(prog)s 视频.mp4
  %(prog)s 视频.mp4 -d 10
  %(prog)s 视频.mp4 -m general --output 报告.md
  %(prog)s 视频.mp4 --yes
        '''
    )
    parser.add_argument('video', help='视频文件路径')
    parser.add_argument('--segment-duration', '-d', type=int, default=15,
                        help='分段时长（秒，默认15）')
    parser.add_argument('--mode', '-m', choices=['reference', 'general', 'technical', 'shot'],
                        default='reference', help='分析模式')
    parser.add_argument('--output', '-o', default=None, help='报告输出路径')
    parser.add_argument('--yes', '-y', action='store_true', help='跳过费用确认')
    parser.add_argument('--no-transcribe', action='store_true', help='跳过音频转写')

    args = parser.parse_args()
    video_path = Path(args.video)

    if not video_path.exists():
        print(f'错误: 视频文件不存在: {video_path}', file=sys.stderr)
        sys.exit(1)

    # ── Init ──
    load_credentials()
    check_env()

    info = get_video_info(str(video_path))
    duration = info['duration']

    cost_est, seg_count = estimate_cost(duration, args.segment_duration)

    print(f'\n{"="*50}')
    print(f'  🎬 影片解释器')
    print(f'{"="*50}')
    print(f'  文件: {video_path.name}')
    print(f'  时长: {int(duration//60)}:{int(duration%60):02d}')
    print(f'  分辨率: {info["width"]}x{info["height"]}')
    print(f'  大小: {info["size"]/1024/1024:.1f}MB')
    print(f'  分段: {seg_count}段 × {args.segment_duration}s')
    print(f'  预估费用: ¥{cost_est}')
    print(f'{"="*50}\n')

    if not args.yes:
        try:
            resp = input('  确认执行? [Y/n]: ').strip().lower()
            if resp == 'n':
                print('  已取消')
                sys.exit(0)
        except EOFError:
            pass

    total_start = time.time()
    total_tokens = 0
    results = {
        'video_name': video_path.name,
        'duration': duration,
        'overview': '',
        'style_analysis': '',
        'transcript': '',
        'segments': [],
        'total_tokens': 0,
        'estimated_cost': cost_est,
    }

    # ── Step 1: Upload to OSS ──
    log_step('1/5', '上传视频到阿里云OSS...')
    video_url, oss_key = upload_to_oss(str(video_path))
    log('上传完成')

    try:
        # ── Step 2: Overview ──
        log_step('2/5', '全片概览分析...')
        try:
            r, usage = call_qwen(video_url, OVERVIEW_PROMPT, max_tokens=6000, timeout=300)
            results['overview'] = r
            total_tokens += usage.get('total_tokens', 0)
            log('完成')
        except Exception as e:
            results['overview'] = f'[分析失败: {e}]'
            log(f'失败: {e}')

        # ── Step 3: Style Analysis ──
        log_step('3/5', '视觉风格分析...')
        try:
            r, usage = call_qwen(video_url, STYLE_PROMPT, max_tokens=4000, timeout=180)
            results['style_analysis'] = r
            total_tokens += usage.get('total_tokens', 0)
            log('完成')
        except Exception as e:
            results['style_analysis'] = f'[分析失败: {e}]'
            log(f'失败: {e}')

        # ── Step 4: Segments ──
        log_step('4/5', f'逐段画面分析 ({seg_count}段)...')
        segments = calc_segments(duration, args.segment_duration)

        with tempfile.TemporaryDirectory() as tmpdir:
            for i, seg in enumerate(segments):
                seg_path = os.path.join(tmpdir, f'seg{seg["index"]:02d}.mp4')
                cut_segment(str(video_path), seg, seg_path)

                seg_url, seg_oss_key = upload_to_oss(seg_path)

                prompt = SEGMENT_PROMPT.format(
                    idx=seg['index'], start=seg['start'], end=seg['end']
                )

                content = ''
                try:
                    r, usage = call_qwen(seg_url, prompt, max_tokens=3000, timeout=180)
                    content = r
                    total_tokens += usage.get('total_tokens', 0)
                except Exception as e:
                    content = f'[分析失败: {e}]'

                results['segments'].append({
                    'index': seg['index'],
                    'start': seg['start'],
                    'end': seg['end'],
                    'analysis': content,
                })

                delete_from_oss(seg_oss_key)

                # Progress indicator
                pct = (i + 1) / seg_count * 100
                bar = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))
                print(f'\r     [{bar}] {i+1}/{seg_count} ({pct:.0f}%)', end='', flush=True)

            print()
            log('逐段分析完成')

        # ── Step 5: Audio Transcription ──
        transcript = None
        if not args.no_transcribe and info['has_audio']:
            log_step('5/5', '音频转文字...')
            audio_path = tempfile.mktemp(suffix='.wav')
            try:
                extract_audio(str(video_path), audio_path)
                transcript = transcribe_audio(audio_path)
                if transcript:
                    results['transcript'] = transcript
                    log(f'完成: {len(transcript)}字符')
                else:
                    results['transcript'] = ''
                    log('转写返回空')
            except Exception as e:
                results['transcript'] = ''
                log(f'失败: {e}')
            finally:
                try: os.remove(audio_path)
                except: pass
        else:
            log_step('5/5', '音频转文字')
            if not info['has_audio']:
                log('跳过: 无音频轨道')
            else:
                log('跳过: --no-transcribe')

    finally:
        delete_from_oss(oss_key)

    # ── Generate Report ──
    results['total_tokens'] = total_tokens

    total_elapsed = time.time() - total_start
    final_cost = (total_tokens * QWEN_INPUT_PRICE / 1000 * 0.66 +
                  total_tokens * QWEN_OUTPUT_PRICE / 1000 * 0.34)
    results['estimated_cost'] = round(final_cost, 4)

    report = generate_report(results)

    # Add stats
    report += f'\n\n---\n**分析统计**  \n'
    report += f'总耗时: {total_elapsed:.0f}秒  \n'
    report += f'Token消耗: {total_tokens}  \n'
    report += f'预估费用: ¥{final_cost:.4f}  \n'

    # ── Output ──
    print(f'\n{"="*50}')
    print(f'  ✅ 分析完成')
    print(f'  耗时: {total_elapsed:.0f}s | Token: {total_tokens} | 费用: ¥{final_cost:.4f}')
    print(f'{"="*50}\n')

    print(report)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = video_path.parent / f'{video_path.stem}_分析报告.md'

    output_path.write_text(report, encoding='utf-8')
    print(f'\n  报告已保存: {output_path.resolve()}\n')


if __name__ == '__main__':
    main()
