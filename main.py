#!/usr/bin/env python3
"""
影片解释器 - macOS 桌面应用
自动启动 Web 服务 + 打开图形界面
"""

import json, os, subprocess, sys, threading, time, uuid, tempfile, base64, socket
from pathlib import Path

try:
    import webview
except ImportError:
    print("安装 pywebview: pip3 install pywebview --break-system-packages")
    sys.exit(1)

try:
    from bottle import Bottle, run, request, response, static_file
except ImportError:
    print("安装 bottle: pip3 install bottle --break-system-packages")
    sys.exit(1)

BASE_DIR = Path(__file__).parent
DESKTOP_DIR = BASE_DIR / 'desktop'

def load_env():
    from dotenv import load_dotenv
    for p in [BASE_DIR / '.env', Path.home() / '.codex' / 'skills' / 'aliyun-isi' / '.env']:
        if p.exists(): load_dotenv(p); return
load_env()

analysis_log = []; analysis_done = False; analysis_result = None; analysis_error = None
api = Bottle()

# ── Prompts ──
SEGMENT_PROMPT = '''这是影片的一个片段（第{idx}段，{start}s - {end}s）。请按以下格式输出分镜表：

| 镜号 | 景别 | 运镜 | 画面内容 | 时长 | 构图参考 | 灯光 | 色彩 | 声音/对白 | 拍摄建议 |
|------|------|------|----------|------|----------|------|------|-----------|----------|
| 1 | 特写 | 固定 | ... | ... | ... | ... | ... | ... | ... |

每个镜头一行。画面内容写具体你在画面中看到了什么。'''

OVERVIEW_PROMPT = '''分析这个影片，输出：

## 影片信息
- 类型：
- 风格：
- 主题：

## 情绪曲线
开始→发展→高潮→结尾

## 视觉总览
色彩·光线·构图·节奏

## 可借鉴手法
1.
2.'''

def fmt(s): return f'{int(s//60)}:{int(s%60):02d}'

# ── API Routes ──
@api.route('/api/status')
def api_status():
    return {'status': 'ok', 'version': '2.1'}

@api.route('/api/progress')
def api_progress():
    return {'logs': analysis_log[-50:], 'done': analysis_done, 'error': analysis_error}

@api.route('/api/result')
def api_result():
    return analysis_result or {'error': '暂无'}

@api.route('/api/analyze', method='POST')
def api_analyze():
    global analysis_log, analysis_done, analysis_result, analysis_error
    if 'video' not in request.files: return {'error': '没有视频'}
    video = request.files['video']
    seg_duration = request.forms.get('segment_duration', '15')
    if not video.filename: return {'error': '无效文件名'}

    file_id = uuid.uuid4().hex[:8]
    safe_name = video.filename.replace(' ', '_')
    video_path = Path('/tmp') / f'va_{file_id}_{safe_name}'
    video.save(str(video_path))

    analysis_log = []; analysis_done = False; analysis_result = None; analysis_error = None

    def add(step, msg):
        analysis_log.append({'step': step, 'msg': msg, 'time': time.time()})

    def extract_frame(vp, t, out, size='320x180'):
        subprocess.run(['ffmpeg', '-ss', str(t), '-i', str(vp), '-vframes', '1', '-s', size, '-y', out], capture_output=True)
        return Path(out).exists()

    def frame_to_data(p):
        with open(p, 'rb') as f: return f'data:image/jpeg;base64,{base64.b64encode(f.read()).decode()}'

    def run():
        global analysis_done, analysis_result, analysis_error
        try:
            add(0, '上传到阿里云OSS...')
            import oss2, requests as req
            auth = oss2.Auth(os.environ['ALIYUN_ACCESS_KEY_ID'], os.environ['ALIYUN_ACCESS_KEY_SECRET'])
            bucket = oss2.Bucket(auth, os.environ['ALIYUN_OSS_ENDPOINT'], os.environ['ALIYUN_OSS_BUCKET'])
            oss_key = f'va/{file_id}_{safe_name}'
            bucket.put_object_from_file(oss_key, str(video_path))
            video_url = bucket.sign_url('GET', oss_key, 7200)
            add(0, '上传完成')

            add(1, '读取信息...')
            r = subprocess.run(['ffprobe', '-v', 'quiet', '-print_format', 'json', '-show_format', str(video_path)], capture_output=True, text=True)
            duration = float(json.loads(r.stdout).get('format', {}).get('duration', 0))
            add(1, f'时长: {int(duration//60)}:{int(duration%60):02d}')

            api_key = os.environ.get('DASHSCOPE_API_KEY') or os.environ.get('BAILIAN_API_KEY')

            def qwen(url, prompt, mt=4000, to=180):
                p = {'model': 'qwen3.7-plus', 'messages': [{'role': 'user', 'content': [{'type': 'video_url', 'video_url': {'url': url}}, {'type': 'text', 'text': prompt}]}], 'max_tokens': mt}
                resp = req.post('https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions', headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}, json=p, timeout=to)
                if resp.status_code != 200: raise Exception(f'API {resp.status_code}')
                r2 = resp.json()
                return r2['choices'][0]['message']['content'], r2.get('usage', {})

            total_tokens = 0

            add(1, '全片概览...')
            try:
                overview, usage = qwen(video_url, OVERVIEW_PROMPT, 4000, 300)
                total_tokens += usage.get('total_tokens', 0)
                add(1, '完成')
            except Exception as e:
                overview = f'[失败: {e}]'
                add(1, f'失败: {e}')

            add(2, '分镜分析+抽帧...')
            seg_dur = int(seg_duration)
            seg_count = max(1, int(duration / seg_dur))
            segments = []

            with tempfile.TemporaryDirectory() as tmpdir:
                for i in range(seg_count):
                    start = i * seg_dur
                    end = min((i + 1) * seg_dur, duration)
                    add(2, f'第 {i+1}/{seg_count} 段 ({fmt(start)}-{fmt(end)})')

                    seg_path = os.path.join(tmpdir, f's{i:02d}.mp4')
                    subprocess.run(['ffmpeg', '-i', str(video_path), '-ss', str(start), '-t', str(end-start), '-c', 'copy', '-y', seg_path], capture_output=True)

                    sk = f'va/{file_id}_s{i:02d}.mp4'
                    bucket.put_object_from_file(sk, seg_path)
                    su = bucket.sign_url('GET', sk, 3600)

                    try:
                        content, usage = qwen(su, SEGMENT_PROMPT.format(idx=i+1, start=start, end=end), 4000, 180)
                        total_tokens += usage.get('total_tokens', 0)
                    except Exception as e:
                        content = f'[失败: {e}]'

                    # Frames
                    seg_frames = []
                    seg_len = end - start
                    pts = []
                    if seg_len >= 10: pts = [start+seg_len*0.25, start+seg_len*0.5, start+seg_len*0.75]
                    elif seg_len >= 5: pts = [start+seg_len*0.5]
                    else: pts = [start]
                    for j, t2 in enumerate(pts):
                        fp = os.path.join(tmpdir, f'f{i:02d}_{j:02d}.jpg')
                        if extract_frame(str(video_path), t2, fp):
                            seg_frames.append({'time': round(t2, 1), 'data_url': frame_to_data(fp)})

                    segments.append({'index': i+1, 'start': start, 'end': end, 'analysis': content, 'frames': seg_frames})
                    try: bucket.delete_object(sk)
                    except: pass
                    add(2, f'完成 {i+1}/{seg_count}')

            # Transcribe
            transcript = ''
            add(3, '音频转文字...')
            try:
                ap = Path('/tmp') / f'{file_id}.wav'
                subprocess.run(['ffmpeg', '-i', str(video_path), '-vn', '-acodec', 'pcm_s16le', '-ar', '16000', '-ac', '1', '-y', str(ap)], capture_output=True, check=True)
                pf = Path.home() / '.codex' / 'skills' / 'aliyun-isi' / 'scripts' / 'transcribe_paraformer_local.py'
                if pf.exists():
                    txt_out = str(ap) + '.txt'
                    subprocess.run(['python3', str(pf), str(ap), '--txt-out', txt_out], capture_output=True, timeout=120)
                    if os.path.exists(txt_out):
                        with open(txt_out, 'r') as f: transcript = f.read().strip()
                        os.remove(txt_out)
                    os.remove(str(ap))
                    add(3, f'完成: {len(transcript)}字')
                else: add(3, '跳过')
            except Exception as e:
                add(3, f'失败: {e}')

            try: bucket.delete_object(oss_key)
            except: pass
            try: os.remove(str(video_path))
            except: pass

            ic = (total_tokens * 0.002 / 1000 * 0.66)
            oc = (total_tokens * 0.008 / 1000 * 0.34)

            analysis_result = {
                'video_name': video.filename, 'duration': duration, 'overview': overview,
                'transcript': transcript, 'segments': segments, 'total_tokens': total_tokens,
                'estimated_cost': round(ic + oc, 4), 'mode': 'storyboard-with-frames',
            }
        except Exception as e:
            analysis_error = str(e)
            import traceback; traceback.print_exc()
        finally:
            analysis_done = True

    threading.Thread(target=run, daemon=True).start()
    return {'status': 'started', 'video': video.filename}

@api.route('/')
@api.route('/<path:path>')
def serve(path='index.html'):
    return static_file(path, root=str(DESKTOP_DIR))

@api.route('/js/<path:path>')
def js(path):
    return static_file(f'js/{path}', root=str(DESKTOP_DIR))

# ── Start ──
def main():
    import mimetypes
    mimetypes.add_type('text/javascript', '.js')
    mimetypes.add_type('text/css', '.css')

    # Auto-find port
    port = 5199
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', port)); s.close()
    except:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]; s.close()

    # Start API server
    threading.Thread(target=lambda: run(api, host='127.0.0.1', port=port, quiet=True, debug=False), daemon=True).start()
    time.sleep(0.5)

    # Try to open webview, fallback to browser
    try:
        webview.create_window(
            title='🎬 影片解释器', url=f'http://127.0.0.1:{port}',
            width=1000, height=780, resizable=True, text_select=True,
        )
        webview.start(debug=False, http_server=False)
    except Exception as e:
        import webbrowser
        webbrowser.open(f'http://127.0.0.1:{port}')
        print(f'WebView不可用，已在浏览器打开: http://127.0.0.1:{port}')
        input('按回车退出...')

if __name__ == '__main__':
    main()
