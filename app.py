# app.py
import os
import logging
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
from werkzeug.utils import secure_filename
from pathlib import Path
import uuid # 用于生成唯一的任务 ID 或文件名
import configparser
import time # 用于生成时间戳文件名

# 从 celery_app 导入 Celery 实例和任务
try:
    from .celery_app import celery_app
    from .tasks import convert_ppt_to_video_task, get_available_tts_voices
    CELERY_AVAILABLE = True
except ImportError as e:
    print(f"FATAL ERROR: 无法导入 Celery 应用或任务: {e}")
    print("请确保 celery_app.py 和 tasks.py 文件存在且无误，并且安装了所有依赖。")
    CELERY_AVAILABLE = False # 标记 Celery 不可用


# --- Flask 应用配置 ---
app = Flask(__name__)
# 在生产环境中，SECRET_KEY 必须是随机且安全的
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'a_very_secret_key_fallback')
# 文件上传和输出目录 (相对于 app.py 所在的目录，或者使用绝对路径)
# 在生产环境（例如 Docker 容器中），这些路径通常需要是容器内的路径，并映射到宿主机卷或云存储
# 这里使用相对于项目根目录的路径作为示例
BASE_DIR = Path(__file__).parent # 项目根目录路径
app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output'
app.config['ALLOWED_EXTENSIONS'] = {'pptx'}
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 限制上传大小为 100MB (根据需要调整)

# --- Celery 配置 (Flask 可以读取，但任务直接从 config.ini 读取更可靠) ---
# Flask 可以通过 app.config['CELERY_BROKER_URL'] = ... 方式配置 Celery，
# 但我们已经通过 celery_app.py 和 config.ini 进行了配置，这里不再重复。

# --- 配置解析器 (由 Flask 应用加载) ---
config = configparser.ConfigParser()
# 假定 config.ini 在项目根目录
config_path = BASE_DIR / 'config.ini'
if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        print(f"[Flask App] 成功加载配置: {config_path}")
    except Exception as e:
        print(f"[Flask App] 错误: 加载配置 {config_path} 失败: {e}")
else:
    print(f"[Flask App] 警告: 配置未找到: {config_path}")

# --- 日志记录配置 (由 Flask 应用加载) ---
# 这是 Flask Web 服务器的日志，与 Celery Worker 的日志是独立的
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
logging.basicConfig(level=log_level, format='%(asctime)s - %(levelname)s - [%(process)d] - %(message)s') # 基础配置

# 确保上传和输出目录存在
app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
app.config['OUTPUT_FOLDER'].mkdir(parents=True, exist_ok=True)

# 获取任务临时目录基础路径，确保 Web 应用有权限创建
# 任务 Worker 最终会使用这个路径，但 Web 应用可能需要创建顶层目录
temp_base_dir = Path(config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp'))
try:
     temp_base_dir.mkdir(parents=True, exist_ok=True)
     logging.info(f"任务临时文件基础目录（由 Web 应用确认）: {temp_base_dir}")
except OSError as e:
     logging.error(f"Web 应用无法创建任务临时文件基础目录 {temp_base_dir}: {e}")
     # 这不是致命错误，但可能导致后续任务失败，需要用户手动创建或调整权限

# --- 辅助函数 ---
def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_config():
    """提供获取配置对象的方法"""
    return config

def get_task_logger_for_web():
    """为 Web 应用的日志提供一个兼容 Celery logger 接口的包装"""
    # 在 Web 应用中，我们使用标准的 logging 模块，但可以通过这个函数获取一个 logger 实例
    # Celery worker 中的 logger 会有任务 ID 等信息，这里只是一个简单的日志器
    return logging.getLogger(__name__) # 返回 Flask app 的日志器

# --- Web 前端需要调用的核心逻辑函数 ---
# 这里可以导入 tasks 中非 @task 装饰的函数，或者 core_logic 中的基础函数
# 例如，获取 TTS 语音列表可能在 Web 前端需要展示
# 确保这些函数在 app.py 环境中可以安全运行（不依赖 Celery 或特定线程）

# 获取 TTS 语音列表函数
if CELERY_AVAILABLE:
    try:
        # 直接从 tasks 导入 (tasks 又从 core_logic 导入)
        # 注意：在 worker 启动前调用这个函数，它内部不应该依赖 Celery 运行时
        from .tasks import get_available_tts_voices # 导入 tasks 中定义的语音列表函数
        logging.info("成功导入获取可用 TTS 语音列表函数。")
        # 获取语音列表供前端使用
        available_voices = get_available_tts_voices(get_task_logger_for_web()) # 在应用启动时获取一次
    except ImportError as e:
         logging.error(f"导入获取可用 TTS 语音列表函数失败: {e}")
         available_voices = []
    except Exception as e:
         logging.error(f"获取可用 TTS 语音列表时发生错误: {e}", exc_info=True)
         available_voices = []
else:
    logging.warning("Celery 不可用，将无法处理任务和获取 TTS 语音列表。")
    available_voices = []


# --- 路由定义 ---

@app.route('/', methods=['GET', 'POST'])
def index():
    """主页：显示上传表单"""
    if request.method == 'POST':
        # 处理文件上传
        if 'pptx_file' not in request.files:
            return redirect(request.url) # 如果没有文件部分，重定向回原页面

        file = request.files['pptx_file']

        if file.filename == '':
            return redirect(request.url) # 如果文件名为空，重定向回原页面

        if file and allowed_file(file.filename):
            # 使用 secure_filename 确保文件名安全
            original_filename = secure_filename(file.filename)
            # 生成一个独特的文件名，包含时间戳，避免覆盖
            timestamp = int(time.time())
            unique_filename = f"{timestamp}_{original_filename}"
            filepath = app.config['UPLOAD_FOLDER'] / unique_filename

            try:
                file.save(filepath)
                logging.info(f"文件已保存: {filepath}")

                # 获取选中的语音 ID
                voice_id = request.form.get('voice_id')
                if not voice_id:
                     # 如果前端没有传 voice_id 或传了空值，使用默认值或第一个可用语音
                     if available_voices:
                          voice_id = available_voices[0].get('id', 'zh-CN-XiaoxiaoNeural') # 使用第一个或一个默认值
                          logging.warning(f"未收到 voice_id，使用默认值: {voice_id}")
                     else:
                          voice_id = 'zh-CN-XiaoxiaoNeural' # 没有可用语音时硬编码一个

                if not CELERY_AVAILABLE:
                     logging.error("Celery 不可用，无法启动转换任务！")
                     # 在生产环境应该返回错误给用户
                     return render_template('index.html', voices=available_voices, error="服务器后台任务处理系统未启动。")


                # --- 启动 Celery 转换任务 ---
                # 将文件路径（字符串）、输出目录（字符串）、语音ID传递给任务
                task = convert_ppt_to_video_task.apply_async(args=[
                    str(filepath.resolve()),
                    str(app.config['OUTPUT_FOLDER'].resolve()),
                    voice_id
                ])
                logging.info(f"Celery 任务已发送，任务 ID: {task.id}")

                # 重定向到任务状态页面
                return redirect(url_for('task_status', task_id=task.id))

            except Exception as e:
                logging.error(f"文件上传或任务发送失败: {e}", exc_info=True)
                # 在生产环境应该返回更友好的错误页面或信息
                return render_template('index.html', voices=available_voices, error=f"文件上传或处理启动失败: {e}")
        else:
            # 文件类型不允许
            logging.warning(f"不允许的文件类型上传: {file.filename}")
            return render_template('index.html', voices=available_voices, error=f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}")

    # GET 请求，显示上传表单
    # 获取可用的语音列表传递给模板
    return render_template('index.html', voices=available_voices)


@app.route('/status/<task_id>')
def task_status(task_id):
    """显示任务状态页面"""
    # 这个页面主要通过 JavaScript 轮询 /tasks/<task_id>/status API 来获取状态
    # 初始渲染时，可以获取一次当前状态显示
    task = celery_app.AsyncResult(task_id)
    return render_template('status.html', task_id=task_id, initial_status=task.state)


@app.route('/tasks/<task_id>/status')
def get_task_status(task_id):
    """API 接口：返回任务的当前状态"""
    if not CELERY_AVAILABLE:
         return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500

    task = celery_app.AsyncResult(task_id)
    response = {
        'state': task.state, # 任务状态 (PENDING, STARTED, SUCCESS, FAILURE)
        'task_id': task.id
    }
    if task.state == 'SUCCESS':
        # 如果成功，task.result 包含任务函数的返回值 (最终视频相对路径)
        response['result'] = task.result
        # 构造下载 URL
        # 假设 /output 路由服务于 output 文件夹
        response['download_url'] = url_for('download_file', filename=Path(task.result).name) # 返回文件名部分构造 URL
    elif task.state == 'FAILURE':
        # 如果失败，task.result 包含异常信息
        response['error'] = str(task.result)
    elif task.state == 'PROCESSING' and task.info:
         # 如果任务在运行中且有自定义状态更新 (如进度条)，可以在 meta 中
         response['meta'] = task.info

    return jsonify(response)


@app.route('/output/<filename>')
def download_file(filename):
    """提供生成的视频文件下载"""
    # 确保 filename 不包含目录穿越的风险
    safe_filename = secure_filename(filename)
    # 从 OUTPUT_FOLDER 目录下提供文件
    try:
        return send_from_directory(app.config['OUTPUT_FOLDER'], safe_filename)
    except FileNotFoundError:
        # 如果文件不存在
        logging.error(f"请求下载的文件未找到: {safe_filename}")
        return "文件未找到。", 404
    except Exception as e:
        logging.error(f"处理文件下载时发生错误: {e}")
        return "文件下载失败。", 500


# --- 错误处理路由 ---
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404 # 如果需要自定义 404 页面

@app.errorhandler(413)
def file_too_large(e):
    return render_template('index.html', voices=available_voices, error=f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024}MB")


# --- 在开发环境中运行 Flask ---
if __name__ == '__main__':
    # 在生产环境应该使用 Gunicorn 或 uWSGI 等 WSGI 服务器来运行 Flask 应用
    # gunicorn --workers 4 --bind 0.0.0.0:8000 app:app
    # celery -A celery_app worker -l info -P eventlet (如果需要异步IO) 或 -P gevent
    # celery -A celery_app worker -l info # 默认进程池

    logging.info("在开发模式下运行 Flask 应用...")
    # debug=True 会提供更多调试信息，但在生产环境应该关闭
    app.run(debug=True, host='0.0.0.0')