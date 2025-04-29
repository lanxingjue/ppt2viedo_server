# app.py
import os
import logging
from flask import Flask, render_template, request, redirect, url_for, send_from_directory, jsonify
from werkzeug.utils import secure_filename
from pathlib import Path
import uuid # 用于生成唯一的任务 ID 或文件名
import configparser
import time # 用于生成时间戳文件名
import sys # 用于检查 Celery/任务导入失败时的退出，并提供更详细错误

# --- 导入 Celery 应用实例和任务函数 ---
# 注意：这里使用绝对导入。假定 celery_app.py 和 tasks.py 文件在项目根目录。
# 如果它们在子目录，例如 myapp/celery_app.py 或 myapp/tasks.py，
# 则导入语句应为 from myapp.celery_app import ... 和 from myapp.tasks import ...
try:
    # 尝试导入 celery_app 模块
    import celery_app
    # 尝试导入 tasks 模块
    import tasks

    # 从导入的模块中获取需要的对象和函数
    celery_app_instance = celery_app.celery_app # 获取 Celery 应用实例
    convert_ppt_to_video_task = tasks.convert_ppt_to_video_task # 获取任务函数
    get_available_tts_voices_web = tasks.get_available_tts_voices # 获取用于 Web 显示的语音列表函数

    CELERY_AVAILABLE = True
    # logging.info("成功导入 Celery 应用和任务模块。") # 启动时会打印日志
except ImportError as e:
    # 如果导入失败，记录并标记 Celery 不可用
    logging.error(f"FATAL ERROR: 无法导入 Celery 应用或任务模块: {e}")
    print(f"FATAL ERROR: 无法导入 Celery 应用或任务模块: {e}") # 打印到控制台
    print("请确保 celery_app.py 和 tasks.py 文件存在且在 Python 搜索路径中，并且安装了所有依赖。") # 打印到控制台
    CELERY_AVAILABLE = False
    # 为了让应用在 Celery 不可用时也能运行（虽然没有核心功能），
    # 我们不在这里 sys.exit(1)，而是在需要 Celery 的地方进行检查。

except Exception as e:
    # 捕获导入过程中可能发生的其他意外错误 (例如，tasks 或 core_logic 中有模块级别的错误)
    logging.error(f"FATAL ERROR: 导入 Celery 应用或任务时发生意外错误: {e}", exc_info=True)
    print(f"FATAL ERROR: 导入 Celery 应用或任务时发生意外错误: {e}") # 打印到控制台
    print("请检查 tasks.py 或其依赖模块中的错误。")
    CELERY_AVAILABLE = False


# --- Flask 应用实例化 ---
app = Flask(__name__)

# --- Flask 应用配置 ---
# 在生产环境中，SECRET_KEY 必须是随机且安全的
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'fallback_secret_key_for_dev_or_testing') # 建议从环境变量获取或使用安全的随机字符串
# 文件上传和输出目录 (相对于 app.py 所在的目录)
BASE_DIR = Path(__file__).parent # 项目根目录路径 (app.py 所在的目录)
app.config['UPLOAD_FOLDER'] = BASE_DIR / 'uploads'
app.config['OUTPUT_FOLDER'] = BASE_DIR / 'output'
app.config['ALLOWED_EXTENSIONS'] = {'pptx'} # 允许上传的文件扩展名
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 限制上传大小为 100MB (字节)

# --- 配置解析器 (由 Flask 应用加载) ---
# 假定 config.ini 在项目根目录
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

# --- 日志记录配置 (由 Flask 应用加载) ---
# 这是 Flask Web 服务器的日志，与 Celery Worker 的日志是独立的
# 在基础配置中已经设置，这里可以根据配置调整级别
log_level_str = config.get('General', 'logging_level', fallback='INFO').upper()
log_level = getattr(logging, log_level_str, logging.INFO)
# logging.basicConfig(level=log_level, ...) # 基础配置通常只调用一次

# 获取任务临时目录基础路径，确保 Web 应用有权限创建
# 任务 Worker 最终会使用这个路径
temp_base_dir = Path(config.get('General', 'base_temp_dir', fallback='/tmp/ppt2video_temp')) # 使用 /tmp 作为 Linux 上的默认回退
try:
     # 确保在 Web 应用运行的用户下有权限创建此目录
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

# 提供获取配置对象的方法（如果需要在路由函数中访问配置）
def get_config():
    return config

# 获取 Flask 应用的日志记录器，用于传递给不需要任务上下文的函数
def get_logger_for_web():
    # 使用 Flask 应用的日志记录器名称
    return logging.getLogger(__name__)


# --- 在应用启动时获取可用语音列表 ---
# 这个列表用于在主页面的下拉框中显示
# 仅在 CELERY_AVAILABLE 为 True 时尝试获取
available_voices = [] # 初始化为空列表
if CELERY_AVAILABLE:
    try:
        # 调用从 tasks 模块导入的函数来获取语音列表
        # get_available_tts_voices_web 需要 logger 参数
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
        # --- 处理文件上传 ---
        if not CELERY_AVAILABLE:
             # 如果 Celery 后台不可用，直接返回错误信息
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
            # 使用 secure_filename 确保文件名安全，并防止目录穿越
            original_filename = secure_filename(file.filename)
            # 生成一个独特的文件名，包含时间戳和 uuid，避免覆盖
            # 确保文件名在不同操作系统中都是有效的
            safe_stem = "".join(c if c.isalnum() or c in ('-', '_') else '_' for c in Path(original_filename).stem)
            timestamp = int(time.time())
            unique_filename = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_stem}{Path(original_filename).suffix}"
            filepath = app.config['UPLOAD_FOLDER'] / unique_filename

            try:
                # 确保上传目录存在（应用启动时已创建，但这里再次确认无害）
                app.config['UPLOAD_FOLDER'].mkdir(parents=True, exist_ok=True)
                # 保存上传的文件
                file.save(filepath)
                logging.info(f"文件已保存: {filepath}")

                # 获取选中的语音 ID
                voice_id = request.form.get('voice_id')
                if not voice_id:
                     # 如果前端没有传 voice_id 或传了空值
                     # 在生产环境，前端应该确保选择了语音，这里做个回退
                     if available_voices:
                          voice_id = available_voices[0].get('id', 'zh-CN-XiaoxiaoNeural') # 使用第一个或一个默认值
                          logging.warning(f"上传请求未收到 voice_id，使用默认值: {voice_id}")
                     else:
                          # 如果没有可用语音，这里即使有 voice_id 也无法处理，但按照逻辑应返回错误
                          # 前端应该禁用开始按钮，如果无可用语音
                           logging.error("无法获取 voice_id，且没有可用语音列表。")
                           return render_template('index.html', voices=available_voices, error="无法获取语音设置，请检查后台服务。")


                # --- 启动 Celery 转换任务 ---
                # 将文件路径（字符串）、最终输出目录（字符串）、语音ID传递给任务
                # 任务函数接收的路径应该是 Celery Worker 环境中可以访问的路径
                task = convert_ppt_to_video_task.apply_async(args=[
                    str(filepath.resolve()), # 传递上传文件的绝对路径
                    str(app.config['OUTPUT_FOLDER'].resolve()), # 传递最终输出目录的绝对路径
                    voice_id # 传递语音 ID
                ])
                logging.info(f"Celery 任务已发送，任务 ID: {task.id}")

                # 重定向到任务状态页面
                return redirect(url_for('task_status', task_id=task.id))

            except Exception as e:
                # 捕获文件保存或任务发送过程中的错误
                logging.error(f"文件上传或任务发送失败: {e}", exc_info=True)
                # 在生产环境应该返回更友好的错误页面或信息
                return render_template('index.html', voices=available_voices, error=f"文件上传或处理启动失败: {e}")
        else:
            # 文件类型不允许
            logging.warning(f"不允许的文件类型上传: {file.filename}")
            return render_template('index.html', voices=available_voices, error=f"只允许上传文件类型为: {', '.join(app.config['ALLOWED_EXTENSIONS'])}")

    # GET 请求，显示上传表单
    # 将在应用启动时获取到的可用语音列表传递给模板
    # 模板会根据 voices 列表是否为空来显示下拉框或提示信息
    return render_template('index.html', voices=available_voices)


@app.route('/status/<task_id>')
def task_status(task_id):
    """显示任务状态页面"""
    # 这个页面主要通过 JavaScript 轮询 /tasks/<task_id>/status API 来获取状态
    # 初始渲染时，可以获取一次当前状态显示
    if not CELERY_AVAILABLE:
         return render_template('status.html', task_id=task_id, initial_status='ERROR', error_message="服务器后台任务处理系统未启动，无法获取状态。")

    task = celery_app_instance.AsyncResult(task_id) # 使用导入的 Celery 实例
    # 传递初始状态给模板，JS 会接着轮询更新
    return render_template('status.html', task_id=task_id, initial_status=task.state)


@app.route('/tasks/<task_id>/status')
def get_task_status(task_id):
    """API 接口：返回任务的当前状态 (JSON 格式)"""
    if not CELERY_AVAILABLE:
         return jsonify({'state': 'ERROR', 'error': 'Celery 后台系统未启动'}), 500

    task = celery_app_instance.AsyncResult(task_id) # 使用导入的 Celery 实例
    response = {
        'state': task.state, # 任务状态 (PENDING, STARTED, SUCCESS, FAILURE 等)
        'task_id': task.id
    }

    # task.info 包含任务通过 self.update_state 发送的额外信息 (如进度)
    if task.info:
         response['meta'] = task.info


    if task.state == 'SUCCESS':
        # 如果成功，task.result 包含任务函数的返回值 (最终视频相对路径字符串)
        # result 是任务成功执行的返回值
        # info 是任务执行过程中通过 update_state 发送的信息
        final_result = task.result
        response['result'] = final_result # 包含任务返回的原始结果

        # 构造下载 URL
        # 假定 task 返回的是相对于 config.ini 中 base_output_dir 的路径
        # download_file 路由服务于 config['General']['base_output_dir'] 目录
        try:
             if final_result: # 确保结果非空
                 # 提取文件名部分来构造下载 URL，防止 result 包含子目录
                 # 需要确保 result 中的文件名与 output 目录下的文件名一致
                 filename_for_download = Path(final_result).name # 只取文件名部分
                 response['download_url'] = url_for('download_file', filename=filename_for_download)
             else:
                  # 任务成功但返回值为空，记录警告
                  logging.warning(f"任务 {task_id} 成功完成，但返回值为空。无法提供下载链接。")
                  response['error'] = "任务成功完成，但未生成有效的下载链接。" # 添加一个错误信息给前端显示
                  response['state'] = 'FAILURE' # 在前端标记为失败，即使任务本身成功
        except Exception as e:
             logging.error(f"为任务 {task_id} 构造下载 URL 失败: {e}", exc_info=True)
             response['error'] = f"构造下载链接失败: {e}"
             response['state'] = 'FAILURE' # 标记为失败


    elif task.state == 'FAILURE':
        # 如果任务失败，task.result 包含异常信息
        response['error'] = str(task.result) # 将异常对象转为字符串

    # 返回 JSON 响应
    return jsonify(response)


@app.route('/output/<filename>')
def download_file(filename):
    """提供生成的视频文件下载"""
    # 从 config.ini 读取基础输出目录
    output_base_dir = Path(config.get('General', 'base_output_dir', fallback='./output'))
    logging.debug(f"尝试从目录 '{output_base_dir}' 提供文件 '{filename}' 下载。")

    # 确保 filename 是安全的，防止目录穿越
    safe_filename = secure_filename(filename)
    # 从配置的基础输出目录提供文件
    try:
        # 使用 send_from_directory 安全地提供文件
        return send_from_directory(output_base_dir, safe_filename, as_attachment=True) # as_attachment=True 会触发浏览器下载而不是预览
    except FileNotFoundError:
        # 如果文件不存在
        logging.error(f"请求下载的文件 '{safe_filename}' 未在目录 '{output_base_dir}' 中找到。")
        return "文件未找到。", 404
    except Exception as e:
        logging.error(f"处理文件 '{safe_filename}' 下载时发生错误: {e}", exc_info=True)
        return "文件下载失败。", 500


# --- 错误处理路由 ---
# 为 404 错误提供一个自定义的模板
@app.errorhandler(404)
def page_not_found(e):
    # 确保 404.html 模板文件存在于 templates 目录
    # 或者可以渲染一个通用的错误模板
    logging.warning(f"收到 404 请求: {request.url}")
    try:
        return render_template('404.html'), 404
    except Exception as render_e:
        logging.error(f"渲染 404 页面失败: {render_e}")
        # 如果渲染模板失败，返回简单文本
        return "404 Not Found: The requested URL was not found on the server.", 404


# 处理文件上传过大的错误
@app.errorhandler(413)
def file_too_large(e):
    logging.warning(f"收到文件过大上传请求: {request.url}")
    # 返回主页，并显示错误信息
    return render_template('index.html', voices=available_voices, error=f"上传文件过大，最大允许 {app.config['MAX_CONTENT_LENGTH'] / 1024 / 1024:.2f}MB"), 413


# --- 在开发环境中运行 Flask ---
if __name__ == '__main__':
    # 在生产环境应该使用 Gunicorn 或 uWSGI 等 WSGI 服务器来运行 Flask 应用
    # 例如: gunicorn --workers 4 --bind 0.0.0.0:8000 app:app

    logging.info("在开发模式下运行 Flask 应用...")
    # debug=True 会提供更多调试信息，并在代码修改时自动重启，方便开发
    # 但在生产环境应该关闭 debug=False
    app.run(debug=True, host='0.0.0.0', port=5000)