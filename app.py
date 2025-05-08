# app.py
import os
import logging
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
from werkzeug.utils import secure_filename
from pathlib import Path
import uuid
import configparser
import time
import sys

# 从 celery_app 导入 Celery 应用实例和任务
try:
    import celery_app
    import tasks

    celery_app_instance = celery_app.celery_app
    convert_ppt_to_video_task = tasks.convert_ppt_to_video_task
    get_available_tts_voices_web = tasks.get_available_tts_voices # 获取用于 Web 显示的语音列表函数

    CELERY_AVAILABLE = True
except ImportError as e:
    logging.error(f"FATAL ERROR: 无法导入 Celery 应用或任务模块: {e}")
    print(f"FATAL ERROR: 无法导入 Celery 应用或任务模块: {e}")
    print("请确保 celery_app.py 和 tasks.py 文件存在且在 Python 搜索路径中，并且安装了所有依赖。")
    CELERY_AVAILABLE = False
except Exception as e:
    logging.error(f"FATAL ERROR: 导入 Celery 应用或任务时发生意外错误: {e}", exc_info=True)
    print(f"FATAL ERROR: 导入 Celery 应用或任务时发生意外错误: {e}")
    print("请检查 tasks.py 或其依赖模块中的错误。")
    CELERY_AVAILABLE = False


# --- Flask 应用实例化 ---
app = Flask(__name__)

# --- Flask 应用配置 ---
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback_secret_key_for_dev_or_testing')
BASE_DIR = Path(__file__).parent
app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output' # 这个 OUTPUT_FOLDER 是 Flask 提供的静态文件服务目录
app.config['ALLOWED_EXTENSIONS'] = {'pptx'}
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024

# --- 配置解析器 ---
config = configparser.ConfigParser()
config_path = BASE_DIR / 'config.ini'
if config_path.exists():
    try:
        config.read(config_path, encoding='utf-8')
        logging.info(f"[Flask App] 成功加载配置: {config_path}")
    except Exception as e:
        logging.error(f"[Flask App] 错误: 加载配置 {config_path} 失败: {e}")
else:
    logging.warning(f"[Flask App] 警告: 配置未找到: {config_path}")

# --- 获取和处理基础输出目录 ---
# 从 config.ini 读取路径，并转换为绝对路径
# 这是 Flask 将用于查找下载文件的基础目录
FLASK_OUTPUT_BASE_DIR = Path(config.get('General', 'base_output_dir', fallback='./output')).resolve()
logging.info(f"Flask 应用将从基础目录 '{FLASK_OUTPUT_BASE_DIR}' 提供文件下载。")

# 确保下载目录存在 (即使 Worker 会创建，Flask 也需要读权限)
try:
    FLASK_OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)
    logging.info(f"确保 Flask 下载基础目录存在: {FLASK_OUTPUT_BASE_DIR}")
except OSError as e:
    logging.error(f"Web 应用无法创建或访问 Flask 下载基础目录 {FLASK_OUTPUT_BASE_DIR}: {e}")


# --- 辅助函数 ---
def allowed_file(filename):
    """检查文件扩展名是否允许"""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in app.config['ALLOWED_EXTENSIONS']

def get_config():
    """提供获取配置对象的方法"""
    return config

# 获取 Flask 应用的日志记录器，用于传递给不需要任务上下文的函数
def get_logger_for_web():
    return logging.getLogger(__name__)


# --- 在应用启动时获取可用语音列表 ---
available_voices = []
if CELERY_AVAILABLE:
    try:
        available_voices = get_available_tts_voices_web(get_logger_for_web())
        logging.info(f"Flask 应用启动时获取到 {len(available_voices)} 个可用语音。")
        if not available_voices:
             logging.warning("获取到的可用语音列表为空。请检查 TTS 配置和服务是否可用。")
    except Exception as e:
         logging.error(f"Flask 应用启动时获取可用 TTS 语音列表失败: {e}", exc_info=True)
         available_voices = []
         logging.warning("无法获取可用语音列表，下拉框将为空。")


# --- 路由定义 ---

@app.route('/', methods=['GET', 'POST'])
def index():
    """主页：显示上传表单和可用语音列表"""
    if request.method == 'POST':
        if not CELERY_AVAILABLE:
             logging.error("收到上传请求，但 Celery 后台系统不可用。")
             return render_template('index.html', voices=available_voices, error="服务器后台任务处理系统未启动，无法处理请求。")

        if 'pptx_file' not in request.files:
            logging.warning("上传请求中没有文件部分。")
            return render_template('index.html', voices=available_voices, error="请选择一个 PPTX 文件上传。")

        file = request.files['pptx_file']

        if file.filename == '':
            logging.warning("上传的文件名为空。")
            return render_template('index.html', voices=available_voices, error="文件名无效，请重新选择文件。")

        if file and allowed_file(file.filename):
            original_filename = secure_filename(file.filename)
            safe_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in Path(original_filename).stem)
            timestamp = int(time.time())
            unique_filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_stem}{Path(original_filename).suffix}"
            filepath = app.config['UPLOAD_FOLDER'] / unique_filename

            try:
                app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
                file.save(filepath)
                logging.info(f"文件已保存: {filepath}")

                voice_id = request.form.get('voice_id')
                if not voice_id:
                     if available_voices:
                          voice_id = available_voices[0].get('id', 'zh-CN-XiaoxiaoNeural')
                          logging.warning(f"上传请求未收到 voice_id，使用默认值: {voice_id}")
                     else:
                           logging.error("无法获取 voice_id，且没有可用语音列表。")
                           return render_template('index.html', voices=available_voices, error="无法获取语音设置，请检查后台服务。")

                # --- 启动 Celery 转换任务 ---
                # 将上传文件路径和 最终输出目录（从 config 获取的绝对路径）传递给任务
                # Worker 会将最终视频保存到这个目录
                task = convert_ppt_to_video_task.apply_async(args=[
                    str(filepath.resolve()), # 上传文件的绝对路径
                    str(FLASK_OUTPUT_BASE_DIR), # <--- 传递来自 config 的绝对路径给 Worker
                    voice_id
                ])
                logging.info(f"Celery 任务已发送，任务 ID: {task.id}")

                # 重定向到任务状态页面
                return redirect(url_for('task_status', task_id=task.id))

            except Exception as e:
                logging.error(f"文件上传或任务发送失败: {e}", exc_info=True)
                return render_template('index.html', voices=available_voices, error=f"文件上传或处理启动失败: {e}")
        else:
            logging.warning(f"不允许的文件类型上传: {file.filename}")
            return render_template('index.html', voices=available_voices, error=f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}")

    # GET 请求，显示上传表单
    return render_template('index.html', voices=available_voices)


@app.route('/status/<task_id>')
def task_status(task_id):
    """显示任务状态页面"""
    if not CELERY_AVAILABLE:
         return render_template('status.html', task_id=task_id, initial_status='ERROR', error_message="服务器后台任务处理系统未启动，无法获取状态。")

    # 在渲染状态页时，先尝试获取一次任务状态
    task = celery_app_instance.AsyncResult(task_id)
    # 获取任务当前的 meta 信息 (如果存在)
    initial_meta = task.info if task.info and isinstance(task.info, dict) else {}

    # 将初始状态和 meta 信息传递给模板
    return render_template('status.html', task_id=task_id, initial_status=task.state, initial_meta=initial_meta)


@app.route('/tasks/<task_id>/status')
def get_task_status(task_id):
    """API 接口：返回任务的当前状态 (JSON 格式)"""
    if not CELERY_AVAILABLE:
         return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500

    task = celery_app_instance.AsyncResult(task_id) # 使用导入的 Celery 实例
    response = {
        'state': task.state, # 任务状态 (PENDING, STARTED, SUCCESS, FAILURE 等)
        'task_id': task.id,
        'meta': task.info if task.info and isinstance(task.info, dict) else {} # 返回任务的 meta 信息
    }

    if task.state == 'SUCCESS':
        # 如果成功，task.result 包含任务函数的返回值 (最终视频相对路径字符串)
        final_result = task.result
        response['result'] = final_result # 包含任务返回的原始结果

        # 构造下载 URL
        try:
             if final_result: # 确保结果非空
                 # result 是任务返回的最终视频路径字符串
                 # 我们假定任务返回的是最终保存文件的绝对路径或相对于 base_output_dir 的相对路径
                 # 在这里，我们总是尝试从返回的路径中提取文件名部分来构造下载 URL
                 filename_for_download = Path(final_result).name # 只取文件名部分
                 # 使用 url_for 构造下载链接，指向 download_file 路由
                 response['download_url'] = url_for('download_file', filename=filename_for_download)
             else:
                  logging.warning(f"任务 {task_id} 成功完成，但返回值为空。无法提供下载链接。")
                  response['error'] = "任务成功完成，但未生成有效的下载链接。"
                  response['state'] = 'FAILURE' # 在前端标记为失败，即使任务本身成功
        except Exception as e:
             logging.error(f"为任务 {task_id} 构造下载 URL 失败: {e}", exc_info=True)
             response['error'] = f"构造下载链接失败: {e}"
             response['state'] = 'FAILURE' # 标记为失败


    elif task.state == 'FAILURE':
        # 如果任务失败，task.result 包含异常信息
        # 这里不再直接将 task.result 赋给 error，因为我们在任务中已经在 meta 里包含了详细错误
        # 如果 meta 中有 error 字段，前端会显示
        # response['error'] = str(task.result) # 任务的原始异常


        # Celery 后端异常处理问题 ('KeyError: exc_type') 可能在这里再次触发
        # 可以尝试捕获并返回一个通用的错误信息，或者依赖前端去处理 meta 中的错误详情
        pass # 错误信息已经在 meta 里了，前端会使用 meta.error

    # 返回 JSON 响应
    return jsonify(response)


@app.route('/output/<filename>')
def download_file(filename):
    """提供生成的视频文件下载"""
    # 从 config.ini 读取基础输出目录 (使用启动时解析好的 FLASK_OUTPUT_BASE_DIR)
    output_base_dir = FLASK_OUTPUT_BASE_DIR # 使用全局变量

    logging.info(f"尝试从基础目录 '{output_base_dir}' 提供文件 '{filename}' 下载。") # 这里的日志现在会打印正确路径

    # 确保 filename 是安全的，防止目录穿越
    safe_filename = secure_filename(filename)
    try:
        # 检查文件是否存在于正确的目录下（调试）
        full_file_path = output_base_dir / safe_filename
        if not full_file_path.exists():
            logging.error(f"在预期下载路径 '{full_file_path}' 未找到文件。")
            # 返回 404 NotFound 异常，让 Flask 处理
            from werkzeug.exceptions import NotFound
            raise NotFound() # 抛出异常，会被 @app.errorhandler(404) 捕获


        # 使用 send_from_directory 安全地提供文件
        return send_from_directory(output_base_dir, safe_filename, as_attachment=True)
    except FileNotFoundError: # send_from_directory 在文件不存在时会抛出
         # 捕获 FileNotFoundError，抛出 NotFound 让 404 handler 处理
         from werkzeug.exceptions import NotFound
         raise NotFound()
    except Exception as e:
         logging.error(f"处理文件 '{safe_filename}' 下载时发生错误: {e}", exc_info=True)
         return "文件下载失败。", 500


# --- 错误处理路由 ---
# 为 404 错误提供一个自定义的模板
@app.errorhandler(404)
def page_not_found(e):
    # 确保 404.html 模板文件存在于 templates 目录
    logging.warning(f"收到 404 请求: {request.url}")
    try:
        return render_template('404.html'), 404
    except Exception as render_e:
        logging.error(f"渲染 404 页面失败: {render_e}", exc_info=True)
        return "404 Not Found: The requested URL was not found on the server.", 404


# 处理文件上传过大的错误
@app.errorhandler(413)
def file_too_large(e):
    logging.warning(f"收到文件过大上传请求: {request.url}")
    # 返回主页，并显示错误信息
    # 需要重新获取可用语音列表，因为它可能不是全局变量
    voices_for_error_page = []
    if CELERY_AVAILABLE:
        try:
            voices_for_error_page = get_available_tts_voices_web(get_logger_for_web())
        except Exception as ve:
             logging.error(f"在处理 413 错误时获取语音列表失败: {ve}", exc_info=True)

    return render_template('index.html', voices=voices_for_error_page, error=f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024:.2f}MB"), 413


# --- 在开发环境中运行 Flask ---
if __name__ == '__main__':
    # 在生产环境应该使用 Gunicorn 或 uWSGI 等 WSGI 服务器来运行 Flask 应用
    # 例如: gunicorn --workers 4 --bind 0.0.0.0:8000 app:app

    logging.info("在开发模式下运行 Flask 应用...")
    app.run(debug=True, host='0.0.0.0', port=5000)