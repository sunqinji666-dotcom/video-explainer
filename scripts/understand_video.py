#!/usr/bin/env python3
"""
影片解释器 - 视频理解工具
Based on qwen3.7-plus multimodal API

快速分析单个视频片段：
    python3 understand_video.py /path/to/video.mp4
    python3 understand_video.py /path/to/video.mp4 --mode composition
    python3 understand_video.py /path/to/video.mp4 --prompt "分析这个视频的调色风格"
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv


# ── Config ──────────────────────────────────────────────
QWEN_MODEL = 'qwen3.7-plus'
QWEN_INPUT_PRICE_PER_1K = 0.002   # 元/千token
QWEN_OUTPUT_PRICE_PER_1K = 0.008  # 元/千token
OSS_COST_ESTIMATE = 0.001          # 元/次上传（几乎可忽略）

SKILL_DIR = Path(__file__).parent.parent


# ── Analysis Mode Prompts ──────────────────────────────
ANALYSIS_MODES = {
    'general': '请深度分析这个视频的内容。描述你看到了什么：画面内容、场景、人物、动作、情绪、构图、色彩、光线等所有你能观察到的细节。',
    'composition': '请分析这个视频的构图技巧。包括：机位角度、景别、构图方式（三分法、对称、框架式等）、主体位置、视觉引导线、前后景层次等。',
    'color': '请分析这个视频的色彩运用。包括：整体色调、色彩对比、色彩情绪、光线质感、色温倾向、色彩搭配等。',
    'editing': '请分析这个视频的剪辑手法。包括：镜头切换节奏、转场方式、镜头时长、叙事结构、节奏感等。',
    'storytelling': '请分析这个视频的叙事方式。包括：故事结构、情绪曲线、视觉叙事技巧、观众引导方式等。',
    'technical': '请分析这个视频的技术细节。包括：拍摄设备推测、镜头参数推测、稳定方式、后期处理痕迹等。',
    'bts': '这是一个拍摄现场的视频。请分析：现场布置、器材配置、工作人员动作、拍摄流程、可能的拍摄目的等。'
}

DELIVERY_PROMPT = '你是一个专业的影视分析专家。用清晰的中文回答，给出有深度、有行业洞察的分析。'


# ── Helpers ────────────────────────────────────────────

def log(msg):
    print(f'[understand] {msg}', flush=True)


def get_video_duration(video_path):
    import subprocess
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', video_path],
            capture_output=True, text=True
        )
        data = json.loads(result.stdout)
        return float(data.get('format', {}).get('duration', 0))
    except:
        return 0


def load_credentials(env_file=None):
    """Load credentials from .env, trying multiple locations"""
    paths_to_try = []

    if env_file:
        paths_to_try.append(Path(env_file))
    # Project-level .env
    paths_to_try.append(SKILL_DIR / '.env')
    # Fallback to aliyun-isi
    paths_to_try.append(Path.home() / '.codex' / 'skills' / 'aliyun-isi' / '.env')

    for p in paths_to_try:
        if p.exists():
            load_dotenv(p)
            log(f'Loaded credentials from: {p}')
            return

    log('Warning: No .env file found. Trying environment variables.')


def call_qwen_api(api_key, video_url, prompt, max_tokens=4000, timeout=180):
    """Call qwen3.7-plus API with video input"""
    import requests

    url = 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions'

    full_prompt = f'{prompt}\n\n{DELIVERY_PROMPT}'

    payload = {
        'model': QWEN_MODEL,
        'messages': [
            {
                'role': 'user',
                'content': [
                    {
                        'type': 'video_url',
                        'video_url': {'url': video_url}
                    },
                    {
                        'type': 'text',
                        'text': full_prompt
                    }
                ]
            }
        ],
        'max_tokens': max_tokens
    }

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json'
    }

    log(f'Calling {QWEN_MODEL} API...')
    start_time = time.time()
    resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
    elapsed = time.time() - start_time

    if resp.status_code != 200:
        raise Exception(f'API error {resp.status_code}: {resp.text}')

    result = resp.json()
    log(f'API completed in {elapsed:.1f}s')

    return result, elapsed


def estimate_cost(usage, elapsed_seconds):
    prompt_tokens = usage.get('prompt_tokens', 0)
    completion_tokens = usage.get('completion_tokens', 0)

    input_cost = (prompt_tokens / 1000) * QWEN_INPUT_PRICE_PER_1K
    output_cost = (completion_tokens / 1000) * QWEN_OUTPUT_PRICE_PER_1K
    total_cost = input_cost + output_cost + OSS_COST_ESTIMATE

    return {
        'prompt_tokens': prompt_tokens,
        'completion_tokens': completion_tokens,
        'total_tokens': usage.get('total_tokens', 0),
        'input_cost_yuan': round(input_cost, 4),
        'output_cost_yuan': round(output_cost, 4),
        'oss_cost_yuan': OSS_COST_ESTIMATE,
        'total_cost_yuan': round(total_cost, 4),
        'elapsed_seconds': round(elapsed_seconds, 2)
    }


def log_usage(log_path, record):
    log_dir = os.path.dirname(log_path)
    os.makedirs(log_dir, exist_ok=True)
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(record, ensure_ascii=False) + '\n')


# ── Main ───────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='影片解释器 - 视频理解工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
使用示例:
  %(prog)s /path/to/video.mp4
  %(prog)s /path/to/video.mp4 --mode composition
  %(prog)s /path/to/video.mp4 --prompt "分析这个视频的剪辑节奏"
  %(prog)s /path/to/video.mp4 --output result.txt
        '''
    )
    parser.add_argument('video', help='视频文件路径')
    parser.add_argument('--prompt', '-p', help='自定义分析提示词')
    parser.add_argument('--mode', '-m', choices=list(ANALYSIS_MODES.keys()),
                        default='general', help='分析模式（默认: general）')
    parser.add_argument('--output', '-o', help='结果输出文件路径')
    parser.add_argument('--json', '-j', action='store_true', help='输出完整JSON响应')
    parser.add_argument('--keep-oss', action='store_true', help='保留OSS临时文件')
    parser.add_argument('--env', default=None, help='.env文件路径')

    args = parser.parse_args()

    # Validate video
    video_path = Path(args.video)
    if not video_path.exists():
        print(f'错误: 视频文件不存在: {video_path}', file=sys.stderr)
        sys.exit(1)

    # Load credentials
    load_credentials(args.env)

    required_keys = [
        'ALIYUN_ACCESS_KEY_ID', 'ALIYUN_ACCESS_KEY_SECRET',
        'ALIYUN_OSS_BUCKET', 'ALIYUN_OSS_ENDPOINT', 'DASHSCOPE_API_KEY'
    ]
    missing = [k for k in required_keys if not os.environ.get(k)]
    if missing:
        print(f'错误: 缺少必要环境变量: {missing}', file=sys.stderr)
        sys.exit(1)

    # Video info
    duration = get_video_duration(str(video_path))
    file_size = video_path.stat().st_size
    log(f'视频: {video_path.name}')
    log(f'时长: {duration:.1f}s | 大小: {file_size/1024/1024:.1f}MB')

    # Setup OSS
    import oss2
    auth = oss2.Auth(
        os.environ['ALIYUN_ACCESS_KEY_ID'],
        os.environ['ALIYUN_ACCESS_KEY_SECRET']
    )
    bucket = oss2.Bucket(
        auth,
        os.environ['ALIYUN_OSS_ENDPOINT'],
        os.environ['ALIYUN_OSS_BUCKET']
    )

    timestamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    video_ext = video_path.suffix
    oss_key = f'video-analyze/{timestamp}{video_ext}'

    start_time = time.time()

    try:
        # Upload
        log(f'Uploading to OSS...')
        bucket.put_object_from_file(oss_key, str(video_path))

        # Signed URL
        video_url = bucket.sign_url('GET', oss_key, 3600)
        log(f'Signed URL generated')

        # Build prompt
        prompt = args.prompt or ANALYSIS_MODES.get(args.mode, ANALYSIS_MODES['general'])

        # Call API
        result, api_elapsed = call_qwen_api(
            os.environ['DASHSCOPE_API_KEY'],
            video_url,
            prompt
        )

        content = result['choices'][0]['message']['content']
        usage = result.get('usage', {})
        cost = estimate_cost(usage, api_elapsed)
        total_elapsed = time.time() - start_time

        # Output
        print('\n' + '=' * 60)
        print('影片理解结果')
        print('=' * 60 + '\n')
        print(content)

        print('\n' + '-' * 60)
        print(f'耗时: {total_elapsed:.1f}s | Token: {cost["total_tokens"]} | 费用: ¥{cost["total_cost_yuan"]}')
        print('-' * 60)

        # Save
        if args.output:
            output_path = Path(args.output)
            if args.json:
                output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            else:
                output_path.write_text(content, encoding='utf-8')
            log(f'结果已保存: {output_path.resolve()}')

        # Log usage
        log_path = SKILL_DIR / 'logs' / 'usage_history.jsonl'
        log_record = {
            'timestamp': datetime.now().isoformat(),
            'video_file': str(video_path),
            'video_duration': duration,
            'video_size_mb': round(file_size / 1024 / 1024, 2),
            'mode': args.mode,
            'tool': 'understand_video',
            'cost': cost,
            'oss_key': oss_key
        }
        log_usage(log_path, log_record)

    finally:
        if not args.keep_oss:
            log(f'Cleaning up OSS...')
            bucket.delete_object(oss_key)


if __name__ == '__main__':
    main()
